from __future__ import annotations

import io
import html
import logging
import re
from datetime import timedelta
from datetime import timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from signal_stack.models import Brief, BriefAction, Company, CrawlRun, Development, Document, ProfileAttribute, Signal, SignalEvidence, Source, utcnow
from signal_stack.providers.llm.brief_synthesis import SignalSynthesis, synthesize_public_signals
from signal_stack.services.security import normalize_url


HEADINGS = [
    "Executive Summary", "Customer Signals", "Competitor Intelligence", "Market Intelligence",
    "Internal Business Intelligence", "Opportunities Identified", "Risks & Threats",
    "Recommended Actions for This Week", "What We Should Watch", "Source Appendix",
]

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|>])")
logger = logging.getLogger(__name__)


def _escape_untrusted(value: object) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return _MARKDOWN_SPECIAL.sub(r"\\\1", html.escape(text, quote=False))


def _safe_url_text(value: str) -> str:
    if value.startswith("internal://"):
        return "Internal upload record"
    try:
        return normalize_url(value)
    except (ValueError, TypeError):
        return "Invalid or unavailable source URL"


def sanitize_persisted_brief(markdown: str) -> str:
    """Render persisted report text as plain Markdown-safe lines with trusted section headings only.

    This also protects reports saved before the escaping fix. Headings are preserved only when
    the complete canonical heading sequence appears exactly once; duplicated/injected structure
    causes every line, including heading syntax, to be rendered as text.
    """
    lines = (markdown or "").splitlines()
    found = [line[3:] for line in lines if line.startswith("## ") and line[3:] in HEADINGS]
    preserve_headings = found == HEADINGS
    next_heading = 0
    rendered = []
    for line in lines:
        if preserve_headings and next_heading < len(HEADINGS) and line == f"## {HEADINGS[next_heading]}":
            rendered.append(line)
            next_heading += 1
        elif not line.strip():
            rendered.append("")
        else:
            rendered.append(_escape_untrusted(line))
    return "\n".join(rendered)


def _signal_rows(session: Session, company_id: int, since, until):
    return session.execute(
        select(Signal, Development, Document, Source)
        .join(Development, Signal.development_id == Development.id)
        .join(Document, Development.document_id == Document.id)
        .outerjoin(Source, Document.source_id == Source.id)
        .where(Signal.company_id == company_id, Signal.status == "active", Signal.created_at >= since, Signal.created_at <= until)
        .order_by(Signal.score.desc(), Signal.created_at.desc())
    ).all()


def _local_datetime(value, timezone_name: str) -> str:
    try:
        zone = ZoneInfo(timezone_name)
        display_zone = timezone_name
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
        display_zone = f"UTC (invalid configured zone: {timezone_name})"
    local = value.replace(tzinfo=datetime_timezone.utc).astimezone(zone)
    return f"{local:%Y-%m-%d %H:%M %Z}"


def _reported_datetime(value, timezone_name: str) -> str:
    return _local_datetime(value, timezone_name) if value else "Unknown"


def _safe_timezone_label(timezone_name: str) -> str:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return _escape_untrusted(timezone_name)
    return timezone_name


def _entry(signal: Signal, dev: Development, doc: Document, source: Source | None, index: int, timezone_name: str) -> str:
    published = _reported_datetime(doc.published_at, timezone_name)
    discovered = _reported_datetime(doc.discovered_at, timezone_name)
    source_title = _escape_untrusted(source.title if source else "Internal upload")
    evidence = _escape_untrusted(" ".join(doc.content.split())[:320])
    return (
        f"{index}. **{_escape_untrusted(dev.what_happened)}** — {_escape_untrusted(dev.signal_type.title())} signal; ranking {signal.score:.2f}.\n"
        f"   - Factual claim as reported by source: {_escape_untrusted(dev.factual_claim or 'Not assessed in this legacy record.')}\n"
        f"   - Evidence excerpt: {evidence}\n"
        f"   - Interpretation: {_escape_untrusted(dev.interpretation or dev.business_implication)}\n"
        f"   - Affected business area: {_escape_untrusted(dev.affected_business_area or 'Unknown')}\n"
        f"   - Recommended action: {_escape_untrusted(dev.recommended_action or 'No action recorded.')}\n"
        f"   - Source: {source_title}; direct URL: {_escape_untrusted(_safe_url_text(doc.canonical_url))}; publication date {published}; event date {_reported_datetime(doc.event_date, timezone_name)}; "
        f"discovery date {discovered}; modification date {_reported_datetime(doc.modified_at, timezone_name)}; verification: {_escape_untrusted(dev.verification_status)} "
        f"(independent confirmation is not inferred). Source reliability {signal.source_reliability:.2f}; "
        f"evidence strength {signal.evidence_strength:.2f}; analysis confidence {dev.analysis_confidence:.2f} "
        f"({dev.analysis_method or 'legacy-unassessed'}): {_escape_untrusted(dev.confidence_rationale or 'No rationale stored.')}; "
        f"signal classifier: {_escape_untrusted(dev.signal_classification_method or 'legacy-unassessed')}.\n"
    )


def generate_brief(session: Session, company_id: int, *, days: int = 7, use_llm: bool = False) -> Brief:
    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Company not found")
    end = utcnow()
    start = end - timedelta(days=days)
    rows = _signal_rows(session, company_id, start, end)
    approved_sources = session.scalar(select(func.count(Source.id)).where(Source.company_id == company_id, Source.status == "approved")) or 0
    successful_crawls = session.scalar(select(func.count(CrawlRun.id)).where(CrawlRun.company_id == company_id, CrawlRun.status == "succeeded", CrawlRun.started_at >= start, CrawlRun.started_at <= end)) or 0
    failed_crawls = session.scalar(select(func.count(CrawlRun.id)).where(CrawlRun.company_id == company_id, CrawlRun.status == "failed", CrawlRun.started_at >= start, CrawlRun.started_at <= end)) or 0
    llm_synthesis: dict[int, SignalSynthesis] = {}
    llm_status = "LLM synthesis not requested; deterministic analysis used."
    if use_llm:
        from signal_stack.config import settings

        public_rows = [
            row for row in rows
            if row[0].category != "internal" and row[3] is not None
            and row[3].source_type != "internal" and row[2].source_id is not None
        ][:8]
        if not public_rows:
            llm_status = "No public signals were eligible; no model request was sent. Internal records stay local."
        elif settings.llm_provider.casefold() != "openrouter" or not settings.openrouter_api_key.strip():
            llm_status = "OpenRouter synthesis unavailable; configure the provider and API key to enable it. Deterministic analysis used."
        else:
            allowed_profile_keys = {"industry", "offerings_observed", "company_description", "customer_segments", "geography"}
            profile_rows = session.scalars(
                select(ProfileAttribute).where(
                    ProfileAttribute.company_id == company_id,
                    ProfileAttribute.key.in_(allowed_profile_keys),
                    ProfileAttribute.status.in_({"source-stated", "browser-stated", "assignment-provided"}),
                )
            ).all()
            public_profile = {item.key: item.value[:240] for item in profile_rows if item.value.strip()}
            synthesis_inputs = [
                {
                    "signal_id": signal.id,
                    "title": dev.what_happened,
                    "category": signal.category,
                    "signal_type": dev.signal_type,
                    "source_type": source.source_type,
                    "source_title": source.title,
                    "excerpt": doc.content[:1800],
                }
                for signal, dev, doc, source in public_rows
            ]
            try:
                llm_synthesis = synthesize_public_signals(
                    synthesis_inputs,
                    company_context={"company_name": company.name, "public_profile": public_profile},
                )
                llm_status = (
                    f"OpenRouter assisted synthesis for {len(llm_synthesis)} public signal(s); each item was retained only "
                    "when its quote matched stored source text. Interpretations are suggestions, not verification."
                )
            except Exception as exc:
                logger.warning("OpenRouter brief synthesis unavailable; deterministic report used (%s)", type(exc).__name__)
                llm_status = "OpenRouter synthesis unavailable; deterministic analysis used."
    if successful_crawls:
        coverage_note = f"Coverage: {approved_sources} approved sources; {successful_crawls} successful crawl/research runs and {failed_crawls} failed runs in the period. A lack of signals is not proof that no events occurred."
        empty_note = "No meaningful evidence-backed signal surfaced in the collected source material; this does not establish that no development occurred."
    elif rows:
        coverage_note = f"Coverage gap: no successful public crawl/research run in the period ({failed_crawls} failed run(s)); {len(rows)} evidence-backed signal(s) were available from stored records, including any internal uploads."
        empty_note = "No evidence-backed signal surfaced in the collected material for this category; this does not establish that no development occurred."
    else:
        coverage_note = f"Coverage gap: {approved_sources} approved sources; no successful crawl/research run in the period ({failed_crawls} failed run(s)). Current business activity cannot be assessed from this database."
        empty_note = "Not assessed: there is no successful source coverage in this period, so this database cannot determine whether developments occurred."
    category_groups = {name: [] for name in ("customers", "competitors", "market", "internal")}
    for row in rows:
        signal, dev, doc, source = row
        category_groups.setdefault(signal.category, []).append(row)
    lines = [f"# Monday Morning Intelligence Brief — {_escape_untrusted(company.name)}", "", f"Reporting window: {_local_datetime(start, company.timezone)} to {_local_datetime(end, company.timezone)} ({_safe_timezone_label(company.timezone)}).", coverage_note, _escape_untrusted(llm_status), "", "## Executive Summary", ""]
    if rows:
        for index, (signal, dev, doc, source) in enumerate(rows[:5], 1):
            priority = "High" if signal.score >= .7 else "Medium" if signal.score >= .5 else "Low"
            synthesis = llm_synthesis.get(signal.id)
            if synthesis and source is not None:
                lines.append(
                    f"{index}. **{_escape_untrusted(synthesis.headline)}** — AI-assisted interpretation of signal #{signal.id}; "
                    f"priority {priority} (stored ranking {signal.score:.2f})."
                )
                lines.append(f"   - Potential implication (model suggestion; unverified): {_escape_untrusted(synthesis.why_it_matters)}")
                lines.append(f"   - Suggested next step: {_escape_untrusted(synthesis.next_step)}")
                if synthesis.watch_for:
                    lines.append(f"   - Watch for: {_escape_untrusted(synthesis.watch_for)}")
                lines.append(
                    f"   - Evidence quote matched to source text: ‘{_escape_untrusted(synthesis.evidence_quote)}’; "
                    f"source: {_escape_untrusted(source.title)} — {_escape_untrusted(_safe_url_text(doc.canonical_url))}; "
                    f"verification: {_escape_untrusted(dev.verification_status)}."
                )
            else:
                why_it_matters = _escape_untrusted(dev.interpretation or dev.business_implication)
                next_step = _escape_untrusted(dev.recommended_action or "Review relevance with the affected team before acting.")
                lines.append(
                    f"{index}. **{_escape_untrusted(dev.what_happened)}** — {_escape_untrusted(dev.signal_type.title())} signal. "
                    f"Why it matters: {why_it_matters} Recommended next step: {next_step} "
                    f"Priority: {priority} (ranking {signal.score:.2f}). Evidence: {_escape_untrusted(doc.title)} — {_escape_untrusted(_safe_url_text(doc.canonical_url))}; "
                    f"verification: {_escape_untrusted(dev.verification_status)}."
                )
    else:
        lines.append(empty_note)
    section_names = {
        "customers": "Customer Signals", "competitors": "Competitor Intelligence",
        "market": "Market Intelligence", "internal": "Internal Business Intelligence",
    }
    for category, heading in section_names.items():
        lines.extend(["", f"## {heading}", ""])
        items = category_groups.get(category, [])
        if items:
            for index, (signal, dev, doc, source) in enumerate(items[:12], 1):
                lines.append(_entry(signal, dev, doc, source, index, company.timezone))
        else:
            lines.append(empty_note)
    opportunities = [row for row in rows if row[1].signal_type == "opportunity"]
    risks = [row for row in rows if row[1].signal_type == "threat"]
    lines.extend(["", "## Opportunities Identified", ""])
    if opportunities:
        for index, (signal, dev, doc, source) in enumerate(opportunities[:10], 1):
            lines.append(f"{index}. **{_escape_untrusted(dev.what_happened)}** — Evidence: {_escape_untrusted(doc.title)} — {_escape_untrusted(_safe_url_text(doc.canonical_url))}. Potential impact: {_escape_untrusted(dev.interpretation or dev.business_implication)} Next step: {_escape_untrusted(dev.recommended_action or 'Review relevance with the affected team.')} Priority: {'High' if signal.score >= .7 else 'Medium' if signal.score >= .5 else 'Low'} (score {signal.score:.2f}).")
        if len(opportunities) < 3:
            lines.append(f"Only {len(opportunities)} evidence-supported opportunity was identified; fewer than three are reported rather than filling this section with speculation.")
    else:
        if not successful_crawls:
            lines.append("Not assessed: public source coverage is incomplete, so no evidence-supported opportunity can be established. No opportunities are manufactured to reach a target count.")
        else:
            lines.append("No evidence-supported opportunity was identified during the covered period; none is manufactured to fill the section.")
    lines.extend(["", "## Risks & Threats", ""])
    if risks:
        for index, (signal, dev, doc, source) in enumerate(risks[:10], 1):
            lines.append(f"{index}. **{_escape_untrusted(dev.what_happened)}** — {_escape_untrusted(doc.title)} — {_escape_untrusted(_safe_url_text(doc.canonical_url))}. Possible consequence: {_escape_untrusted(dev.interpretation or dev.business_implication)} Action: {_escape_untrusted(dev.recommended_action or 'Review possible exposure with the affected team.')} Urgency: {'High' if signal.urgency >= .75 else 'Moderate' if signal.urgency >= .5 else 'Low'}; the event is not independently verified.")
    else:
        if not successful_crawls:
            lines.append("No evidence-supported risk can be assessed because public source coverage is incomplete.")
        else:
            lines.append("No evidence-supported risk or threat was identified in the collected evidence during this period.")
    action_rows = []
    lines.extend(["", "## Recommended Actions for This Week", ""])
    for signal, dev, doc, source in rows[:5]:
        synthesis = llm_synthesis.get(signal.id)
        action = _escape_untrusted(
            synthesis.next_step if synthesis else
            dev.recommended_action or f"Review the development reported as '{dev.what_happened[:180]}' and determine whether it changes current priorities."
        )
        priority = "high" if signal.score >= .7 else "medium" if signal.score >= .5 else "low"
        owner = {"customers": "Customer / product lead", "competitors": "Strategy / product lead", "market": "Founder / strategy", "internal": "Relevant business owner"}.get(signal.category, "Founder / leadership")
        lines.append(f"- **{priority.title()} — {action}** Reason: {_escape_untrusted(dev.business_implication)} Supporting signal: {_escape_untrusted(doc.title)} — {_escape_untrusted(_safe_url_text(doc.canonical_url))}. Suggested owner: {owner}. Status: Not started.")
        action_rows.append((signal, action, priority, owner))
    if not action_rows:
        lines.append("No action is recommended without an evidence-backed signal.")
    lines.extend(["", "## What We Should Watch", ""])
    if rows:
        for signal, dev, doc, source in rows[:8]:
            watch = "changes affecting customers or operations" if signal.category in {"customers", "internal"} else "a follow-on announcement or measurable market impact"
            lines.append(f"- **{_escape_untrusted(dev.what_happened)}** — Monitor {watch} because {_escape_untrusted(dev.interpretation or dev.business_implication)} Evidence: {_escape_untrusted(doc.title)} — {_escape_untrusted(_safe_url_text(doc.canonical_url))}.")
    else:
        lines.append("No early development was available to carry forward. A future run can assess current activity after approved sources complete successful checks.")
    lines.extend(["", "## Source Appendix", ""])
    if rows:
        for index, (signal, dev, doc, source) in enumerate(rows, 1):
            lines.append(f"{index}. Original source: {_escape_untrusted(source.title if source else 'Internal upload')}; direct URL: {_escape_untrusted(_safe_url_text(doc.canonical_url))}; publication date: {_reported_datetime(doc.published_at, company.timezone)}; event date: {_reported_datetime(doc.event_date, company.timezone)}; discovery date: {_reported_datetime(doc.discovered_at, company.timezone)}; modification date: {_reported_datetime(doc.modified_at, company.timezone)}; evidence: '{_escape_untrusted(' '.join(doc.content.split())[:250])}'; verification: {_escape_untrusted(dev.verification_status)}; source reliability: {signal.source_reliability:.2f}; evidence strength: {signal.evidence_strength:.2f}; analysis confidence: {dev.analysis_confidence:.2f} ({_escape_untrusted(dev.analysis_method)}) — {_escape_untrusted(dev.confidence_rationale)}; signal classifier: {_escape_untrusted(dev.signal_classification_method)}; affected area: {_escape_untrusted(dev.affected_business_area)}; recommended action: {_escape_untrusted(dev.recommended_action)}; signal #{signal.id}.")
    else:
        lines.append("No source appendix entries: no evidence-backed signals were collected in this reporting period.")
    markdown = "\n".join(lines)
    brief = Brief(company_id=company_id, period_start=start, period_end=end, markdown=markdown, signal_count=len(rows))
    session.add(brief)
    session.flush()
    for signal, action, priority, owner in action_rows:
        session.add(BriefAction(brief_id=brief.id, signal_id=signal.id, action=action, priority=priority, owner=owner))
    session.commit()
    return brief


def export_pdf(markdown: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=letter, rightMargin=0.65 * inch, leftMargin=0.65 * inch, topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    story = []
    for raw in markdown.splitlines():
        if not raw.strip():
            story.append(Spacer(1, 5))
        else:
            text = escape(raw)
            if text.startswith("# "):
                story.append(Paragraph(text[2:], styles["Title"]))
            elif text.startswith("## "):
                story.append(Paragraph(text[3:], styles["Heading2"]))
            else:
                text = text.replace("**", "")
                story.append(Paragraph(text, styles["BodyText"]))
    doc.build(story)
    return output.getvalue()


def brief_history(session: Session, company_id: int) -> list[Brief]:
    return list(session.scalars(select(Brief).where(Brief.company_id == company_id).order_by(Brief.created_at.desc())).all())

