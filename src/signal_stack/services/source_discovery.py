from __future__ import annotations

import json
from datetime import timedelta
import re
from urllib.parse import quote_plus, unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_stack.models import Company, Competitor, Source, SourceAudit, utcnow
from signal_stack.services.company_research import profile_values, save_profile_version


CATEGORIES = ("customers", "competitors", "market", "internal")
_STOP_WORDS = {"about", "after", "also", "among", "because", "company", "business", "customers", "from", "have", "into", "more", "most", "other", "that", "their", "there", "these", "this", "which", "with", "your"}


def assess_source_relevance(source: Source, profile: dict[str, str], *, content: str = "") -> float:
    """Score evidence and source identity only; discovery prose is not evidence."""
    profile_text = " ".join(value for key, value in profile.items() if key not in {"company_name", "website"})
    profile_terms = {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", profile_text) if word.lower() not in _STOP_WORDS}
    domain = (urlsplit(source.url).hostname or "").casefold()
    source_text = unquote(f"{source.title} {source.url} {domain} {source.category} {source.source_type} {content[:5000]}")
    source_terms = {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", source_text) if word.lower() not in _STOP_WORDS}
    if not profile_terms:
        baseline = {"internal": .65, "website": .7}.get(source.source_type, .35)
        return round(baseline, 3)
    overlap = len(profile_terms & source_terms)
    match = overlap / max(1, min(12, len(profile_terms)))
    category_floor = {"customers": .3, "competitors": .3, "market": .32, "internal": .65}.get(source.category, .3)
    if source.source_type == "website" and "official website" in source.title.lower():
        category_floor = .65
    return round(min(1.0, category_floor + .68 * match), 3)


def explain_source_relevance(source: Source, profile: dict[str, str], *, content: str = "") -> tuple[float, str]:
    score = assess_source_relevance(source, profile, content=content)
    profile_text = " ".join(value for key, value in profile.items() if key not in {"company_name", "website"})
    profile_terms = {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", profile_text) if word.lower() not in _STOP_WORDS}
    source_text = unquote(f"{source.title} {source.url} {source.category} {source.source_type} {content[:5000]}")
    matches = sorted(profile_terms & {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", source_text) if word.lower() not in _STOP_WORDS})
    why = ", ".join(matches[:8]) if matches else "no matching profile terms in source identity or collected content"
    return score, f"Source relevance {score:.2f}; based on title, URL/domain, source category/type, and collected content. Matches: {why}. Source reliability is a separate measure."


def _query_text(profile: dict[str, str], company: Company) -> dict[str, str]:
    industry = profile.get("industry") or profile.get("classified_industry") or ""
    sub_industry = profile.get("sub_industry") or ""
    subject = profile.get("company_description") or profile.get("offerings_observed") or industry or company.name
    segment = profile.get("customer_segments") or profile.get("interview_segments") or "customers"
    problem = profile.get("customer_problem") or profile.get("interview_customer_needs") or "customer needs"
    geography = profile.get("geography") or "market"
    alternatives = profile.get("competitors") or profile.get("interview_alternatives") or "alternatives"
    return {
        "customers": f'"{problem[:100]}" ({segment[:80]} feedback OR discussion OR questions)',
        "competitors": f'"{company.name}" ({alternatives[:100]} {industry[:60]} competitor OR launch OR pricing)',
        "market": f'({industry[:70]} {sub_industry[:70]} {subject[:100]}) ({geography[:80]} policy OR regulation OR industry OR market)',
        "industry": f'"{industry[:100]}" ({sub_industry[:80]} {subject[:100]}) industry research OR policy OR funding',
    }


def upsert_source(session: Session, company_id: int, *, url: str, title: str, category: str, source_type: str, reason: str, reliability: float = 0.45, relevance: float | None = None) -> Source:
    row = session.scalar(select(Source).where(Source.company_id == company_id, Source.url == url))
    if row:
        row.title = title[:500]
        row.category = category
        row.source_type = source_type
        row.reason = reason
        row.reliability = min(1.0, max(0.0, float(reliability)))
    else:
        row = Source(company_id=company_id, url=url, title=title[:500], category=category, source_type=source_type, reason=reason, reliability=reliability)
        session.add(row)
    row.reliability = min(1.0, max(0.0, float(reliability)))
    if relevance is None:
        row.relevance, row.relevance_reason = explain_source_relevance(row, profile_values(session, company_id))
    else:
        row.relevance = min(1.0, max(0.0, float(relevance)))
        row.relevance_reason = "Explicit discovery relevance supplied by the source-discovery workflow. Reliability is stored separately."
    return row


def discover_sources(session: Session, company_id: int) -> list[Source]:
    """Create clearly unverified candidates from company context; approval stays manual."""
    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Company not found")
    profile = profile_values(session, company_id)
    from signal_stack.services.classification import classify_company_context
    classification = classify_company_context(session, company_id)
    if classification.industry:
        profile["classified_industry"] = classification.industry
    if classification.sub_industry:
        profile["sub_industry"] = classification.sub_industry
    if classification.business_model:
        profile["business_model"] = classification.business_model
    for json_field, profile_key in (("products_services_json", "classified_products"), ("customer_segments_json", "classified_segments")):
        values = json.loads(getattr(classification, json_field) or "[]")
        if values:
            profile[profile_key] = "; ".join(values)
    queries = _query_text(profile, company)
    candidates = [
        upsert_source(
            session, company_id, url=company.website, title=f"{company.name} — official website",
            category="market", source_type="website",
            reason="First-party source for the company’s stated products, positioning, and changes; facts are attributable to the company itself.",
            reliability=0.75,
            relevance=0.75,
        ),
    ]
    for category in ("customers", "competitors", "market"):
        query = quote_plus(queries[category])
        if category == "customers":
            url = f"https://www.reddit.com/search.rss?q={query}&sort=new"
            title = f"Public customer discussions — {company.name} problem space"
            reason = f"Searches public discussion for the customer problem/segment captured in the profile: {queries[category]}. Candidate only; access and relevance must be checked before approval."
            kind, reliability = "search_feed", 0.4
        else:
            url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
            title = f"Public news search — {category.title()} — {company.name}"
            reason = f"A company-specific public news query based on profile context: {queries[category]}. Results require item-level validation; aggregator coverage is not proof."
            kind, reliability = "search_feed", 0.5
        candidates.append(upsert_source(session, company_id, url=url, title=title, category=category, source_type=kind, reason=reason, reliability=reliability))
    if classification.industry:
        query = quote_plus(queries["industry"])
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        candidates.append(upsert_source(
            session, company_id, url=url, title=f"Industry research — {classification.industry}", category="market",
            source_type="search_feed",
            reason=f"Context-derived search for {classification.industry} based on company profile evidence ({classification.confidence:.2f} classification confidence). It is an unapproved discovery feed, not a vetted publication; review results and access before approval.",
            reliability=.45,
        ))
    if classification.regulatory_environment_json != "[]":
        regulations = "; ".join(json.loads(classification.regulatory_environment_json))
        query = quote_plus(f'"{regulations[:100]}" "{company.name}" policy rule guidance')
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        candidates.append(upsert_source(
            session, company_id, url=url, title=f"Regulatory updates — {classification.industry or 'company context'}", category="market",
            source_type="search_feed", reason=f"Search query grounded in the supplied regulatory context: {regulations[:240]}. Candidate only; verify jurisdiction and source authority.", reliability=.45,
        ))
    internal_url = f"internal://company/{company_id}/uploads"
    candidates.append(upsert_source(
        session, company_id, url=internal_url, title="Internal uploads — customer and operating evidence",
        category="internal", source_type="internal",
        reason="Collects only files explicitly supplied by the company, such as CRM exports, support themes, sales notes, or product feedback. No external data is implied.",
        reliability=0.65,
        relevance=0.65,
    ))
    session.commit()
    return candidates


def reassess_sources(session: Session, company_id: int, reason: str) -> list[SourceAudit]:
    """Save a profile version, refresh candidate reasons, and record review recommendations."""
    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Company not found")
    save_profile_version(session, company_id, reason)
    previous_rows = session.scalars(select(Source).where(Source.company_id == company_id)).all()
    previous = {s.id: {"reason": s.reason, "relevance": s.relevance, "status": s.status} for s in previous_rows}
    previous_ids = set(previous)
    candidates = discover_sources(session, company_id)
    audits = []
    current_profile = profile_values(session, company_id)
    for source in session.scalars(select(Source).where(Source.company_id == company_id)).all():
        old = previous.get(source.id)
        old_reason = old["reason"] if old else None
        old_relevance = old["relevance"] if old else None
        from signal_stack.models import Document
        latest_doc = session.scalar(select(Document).where(Document.company_id == company_id, Document.source_id == source.id).order_by(Document.discovered_at.desc(), Document.id.desc()))
        source.relevance, source.relevance_reason = explain_source_relevance(source, current_profile, content=latest_doc.content if latest_doc else "")
        if source.status == "approved" and old_relevance is not None and source.relevance <= old_relevance - .15:
            status = "relevance-declined"
            recommendation = f"Source relevance fell from {old_relevance:.2f} to {source.relevance:.2f} after the profile update ({reason}). Review fit before the next reporting cycle."
        elif source.status == "approved" and old_reason and old_reason != source.reason:
            status = "review-needed"
            recommendation = f"Review because company context changed ({reason}). Previous fit: {old_reason[:300]} Current fit: {source.reason[:300]}"
        elif source.status == "approved" and not old_reason:
            status = "review-needed"
            recommendation = f"Reassess this source after context change ({reason}); relevance was not captured in the previous profile snapshot."
        elif source.status == "candidate":
            status = "candidate"
            recommendation = f"Review candidate against the updated profile ({reason}). Approval requires source and relevance validation."
        else:
            continue
        audit = SourceAudit(company_id=company_id, source_id=source.id, status=status, recommendation=recommendation)
        session.add(audit)
        audits.append(audit)
        if source.id not in previous_ids and source.status == "candidate":
            audit = SourceAudit(company_id=company_id, source_id=source.id, status="candidate-addition", recommendation=f"New source candidate derived from the updated company context ({reason}). Validate availability, access rules, and relevance before approval.")
            session.add(audit)
            audits.append(audit)
    session.commit()
    return audits


def set_source_status(
    session: Session, company_id: int, source_id: int, status: str, *, reviewer_identity: str | None = None
) -> Source:
    if status not in {"approved", "candidate", "rejected", "paused", "removed"}:
        raise ValueError("Unsupported source status")
    source = session.scalar(select(Source).where(Source.id == source_id, Source.company_id == company_id))
    if not source:
        raise ValueError("Source not found for this company")
    if status == "approved" and source.source_type != "internal" and source.status != "approved":
        if source.last_status != "healthy" or not source.last_checked_at or source.last_checked_at < utcnow() - timedelta(days=7):
            raise ValueError("Validate this public source successfully within the last 7 days before approval")
    was_approved = source.status == "approved"
    previous_status = source.status
    source.status = status
    if status == "approved" and not was_approved:
        source.next_due_at = utcnow()
    if status != previous_status:
        actor = " ".join((reviewer_identity or session.info.get("principal", "local-reviewer")).split())[:240]
        if not actor:
            raise ValueError("Reviewer identity is required to change a source status")
        session.add(SourceAudit(
            company_id=company_id, source_id=source.id, status=f"review-{status}", previous_status=previous_status,
            actor_identity=actor,
            recommendation=f"Source review status changed from {previous_status} to {status}.",
        ))
    session.commit()
    return source


def approve_validated_sources(session: Session, company_id: int, source_ids: list[int]) -> list[Source]:
    """Approve a reviewed batch only when each public candidate passed a recent check."""
    ids = list(dict.fromkeys(source_ids))
    if not ids:
        return []
    sources = session.scalars(select(Source).where(Source.company_id == company_id, Source.id.in_(ids))).all()
    if len(sources) != len(ids):
        raise ValueError("One or more sources were not found for this company")
    cutoff = utcnow() - timedelta(days=7)
    reviewer = " ".join(str(session.info.get("principal", "local-reviewer")).split())[:240]
    if not reviewer:
        raise ValueError("Reviewer identity is required to approve sources")
    for source in sources:
        if source.source_type == "internal":
            raise ValueError("Internal sources are managed through the upload workflow")
        if source.status not in {"candidate", "approved"}:
            raise ValueError(f"Source {source.id} is not an approvable candidate")
        if source.last_status != "healthy" or not source.last_checked_at or source.last_checked_at < cutoff:
            raise ValueError(f"Source {source.id} must pass public validation within the last 7 days")
    for source in sources:
        was_approved = source.status == "approved"
        previous_status = source.status
        source.status = "approved"
        if not was_approved:
            source.next_due_at = utcnow()
            session.add(SourceAudit(
                company_id=company_id, source_id=source.id, status="review-approved", previous_status=previous_status,
                actor_identity=reviewer,
                recommendation="Source was approved in a validated batch after recent public access checks and human review.",
            ))
    session.commit()
    return sources


def validate_source(session: Session, company_id: int, source_id: int) -> tuple[Source, SourceAudit]:
    """Check robots/access and a small response body before a candidate can be approved."""
    source = session.scalar(select(Source).where(Source.id == source_id, Source.company_id == company_id))
    if not source:
        raise ValueError("Source not found for this company")
    if source.source_type == "internal":
        source.last_checked_at = utcnow()
        source.last_status = "internal-only"
        audit = SourceAudit(company_id=company_id, source_id=source.id, status="internal-only", recommendation="This internal source is processed only through explicit local file uploads.")
    else:
        try:
            from signal_stack.services.security import fetch_public_url
            response = fetch_public_url(source.url, max_bytes=256_000)
            source.last_status = "healthy"
            source.last_error = None
            source.last_checked_at = utcnow()
            content_type = next((v for k, v in response.headers.items() if k.lower() == "content-type"), "unspecified")
            audit = SourceAudit(company_id=company_id, source_id=source.id, status="candidate-healthy", recommendation=f"Passed public URL, robots.txt, and response checks on {_format_check_time(source.last_checked_at)} (HTTP {response.status_code}, {content_type}). Review the source content and relevance before approval.")
        except Exception as exc:
            source.last_status = "failed"
            source.last_error = str(exc)[:2000]
            source.last_checked_at = utcnow()
            audit = SourceAudit(company_id=company_id, source_id=source.id, status="candidate-failed", recommendation=f"Validation failed: {str(exc)[:500]}. Do not approve until access and relevance are resolved.")
    session.add(audit)
    session.commit()
    return source, audit


def _format_check_time(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def add_competitor(session: Session, company_id: int, name: str, rationale: str, evidence_url: str | None = None) -> Competitor:
    clean = " ".join(name.strip().split())
    if not clean:
        raise ValueError("Competitor or substitute name is required")
    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Company not found")
    existing = session.scalar(select(Competitor).where(Competitor.company_id == company_id, Competitor.name == clean))
    if existing:
        return existing
    competitor = Competitor(company_id=company_id, name=clean, rationale=rationale.strip() or "Added from founder-provided context; verify before treating as fact.", evidence_url=evidence_url, status="candidate")
    session.add(competitor)
    session.commit()
    return competitor

