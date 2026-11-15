from __future__ import annotations

import re
import logging
from difflib import SequenceMatcher
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_stack.models import (
    Company, CompanyClassification, CrawlRun, Development, Document, ProfileAttribute, Signal, SignalEvidence, Source, utcnow,
)
from signal_stack.services.extraction import content_hash, extract_documents
from signal_stack.services.analysis_pipeline import AnalysisContext, EvidenceRecord, analyze_evidence
from signal_stack.services.ranking import _tokens, load_ranking_weights, score_signal
from signal_stack.services.security import fetch_public_url


logger = logging.getLogger(__name__)


THREAT_WORDS = {"ban", "breach", "complaint", "decline", "delay", "fine", "fraud", "investigation", "lawsuit", "layoff", "penalty", "recall", "risk", "scam", "shutdown", "suspension", "threat", "violation", "warning"}
OPPORTUNITY_WORDS = {"acquire", "expansion", "funding", "growth", "launch", "partnership", "partnered", "rollout", "investment", "opportunity"}


def _profile_text(session: Session, company_id: int) -> str:
    attributes = session.scalars(select(ProfileAttribute).where(ProfileAttribute.company_id == company_id)).all()
    return " ".join(f"{row.key} {row.value}" for row in attributes)


def classify(title: str, content: str, category: str = "market") -> str:
    words = _tokens(f"{title} {content[:1000]}")
    has_threat = bool(words & THREAT_WORDS)
    has_opportunity = bool(words & OPPORTUNITY_WORDS)
    if has_threat and has_opportunity:
        return "neutral"
    if has_threat:
        return "threat"
    if category == "internal":
        return "neutral"
    if words & OPPORTUNITY_WORDS:
        return "opportunity"
    return "neutral"


def classify_with_model_details(title: str, content: str, category: str) -> tuple[str, str]:
    """Use configured OpenRouter decision model for public evidence; safely fall back locally."""
    fallback = classify(title, content, category)
    from signal_stack.config import settings
    words = _tokens(f"{title} {content[:1000]}")
    if words & THREAT_WORDS and words & OPPORTUNITY_WORDS:
        return "neutral", "deterministic-mixed-evidence"
    if settings.llm_provider != "openrouter" or not settings.openrouter_api_key or category == "internal":
        return fallback, "deterministic-rules"
    try:
        from signal_stack.providers.llm.openrouter import classify_signal
        return classify_signal(title, content, category), "openrouter-decisions"
    except Exception as exc:
        logger.warning("OpenRouter signal classification failed; using deterministic fallback (%s)", type(exc).__name__)
        return fallback, "deterministic-fallback"


def classify_with_model(title: str, content: str, category: str) -> str:
    return classify_with_model_details(title, content, category)[0]


def _implication(session: Session, company_id: int, source: Source, title: str, signal_type: str) -> str:
    if source.category == "internal":
        return (
            f"This insight comes from an explicitly uploaded internal file. The title reflects deterministic text patterns ({title[:220]}), "
            "not an independently verified trend. Review source dates, sample size, and original rows before changing a decision."
        )
    profile = session.scalars(select(ProfileAttribute).where(ProfileAttribute.company_id == company_id)).all()
    relevant = next((row for row in profile if row.key in {"interview_growth", "interview_decisions", "interview_customer_needs", "company_description", "customer_segments"} and row.value), None)
    context = f" The company profile says: {relevant.value[:260]}" if relevant else ""
    type_text = {"opportunity": "possible opportunity", "threat": "possible risk", "neutral": "developing market signal"}[signal_type]
    return (
        f"This {type_text} may warrant review because the item was found in the {source.category} source stack. "
        f"The headline is evidence of what this source reports, not independent confirmation of the underlying event.{context} "
        "Validate the source and fit with current priorities before taking action."
    )


def _analyze_development(
    session: Session, company_id: int, source: Source, title: str, content: str,
    signal_type: str, published_at,
) -> dict[str, object]:
    """Run evidence, company-impact, recommendation, and confidence stages independently."""
    attributes = session.scalars(select(ProfileAttribute).where(ProfileAttribute.company_id == company_id)).all()
    preferred = ("customer_segments", "customer_problem", "offerings_observed", "company_description", "industry", "interview_growth", "interview_decisions")
    by_key = {row.key: row for row in attributes}
    context_row = next((by_key[key] for key in preferred if key in by_key and by_key[key].value.strip()), None)
    classification = session.scalar(select(CompanyClassification).where(CompanyClassification.company_id == company_id).order_by(CompanyClassification.created_at.desc(), CompanyClassification.id.desc()))
    result = analyze_evidence(
        EvidenceRecord(
            source_title=source.title,
            source_url=source.url,
            category=source.category,
            title=title,
            content=content,
            published_at=published_at,
        ),
        AnalysisContext(
            company_name=session.get(Company, company_id).name,
            profile_key=context_row.key if context_row else None,
            profile_value=context_row.value if context_row else None,
            profile_status=context_row.status if context_row else None,
            profile_evidence_url=context_row.evidence_url if context_row else None,
            industry=classification.industry if classification else None,
            industry_provider=classification.provider if classification else None,
            industry_confidence=classification.confidence if classification else None,
        ),
        signal_type,
    )
    return {
        "factual_claim": result.factual_claim,
        "interpretation": result.interpretation,
        "affected_business_area": result.affected_business_area,
        "recommended_action": result.recommended_action,
        "analysis_confidence": result.analysis_confidence,
        "confidence_rationale": result.confidence_rationale,
        "analysis_method": result.analysis_method,
    }


def _material_content_change(previous: str, current: str) -> bool:
    before = _tokens(previous)
    after = _tokens(current)
    token_delta = len(before.symmetric_difference(after))
    similarity = SequenceMatcher(None, " ".join(previous.split()).casefold(), " ".join(current.split()).casefold(), autojunk=False).ratio()
    return token_delta >= 6 and similarity < 0.92


def _is_homepage(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.path.rstrip("/") == ""


def _persist_documents(session: Session, company_id: int, source: Source, docs) -> tuple[int, int]:
    new_count = 0
    signal_count = 0
    profile = _profile_text(session, company_id)
    for item in docs:
        text = item.content.strip()
        if not item.title.strip() or not text:
            continue
        digest = content_hash(f"{item.title}\n{text}")
        same_version = session.scalar(
            select(Document).where(Document.company_id == company_id, Document.canonical_url == item.url, Document.content_hash == digest)
        )
        if same_version:
            same_version.last_seen_at = utcnow()
            continue
        prior = session.scalar(select(Document).where(Document.company_id == company_id, Document.canonical_url == item.url).order_by(Document.version.desc()))
        version = (prior.version + 1) if prior else 1
        is_homepage = source.source_type == "website" and _is_homepage(item.url)
        material_change = bool(prior and _material_content_change(prior.content, text))
        doc = Document(
            company_id=company_id, source_id=source.id, canonical_url=item.url, title=item.title,
            published_at=item.published_at, event_date=item.event_date, modified_at=item.modified_at,
            is_article=item.is_article, discovered_at=utcnow(), content_hash=digest, content=text,
            category=source.category, source_reliability=source.reliability, version=version,
        )
        session.add(doc)
        session.flush()
        new_count += 1
        duplicate = session.scalar(
            select(Document).where(Document.company_id == company_id, Document.content_hash == digest, Document.id != doc.id).order_by(Document.discovered_at.asc())
        )
        if duplicate:
            existing_signal = session.scalar(
                select(Signal).join(Development, Signal.development_id == Development.id).where(Development.document_id == duplicate.id)
            )
            if existing_signal:
                session.add(SignalEvidence(signal_id=existing_signal.id, document_id=doc.id, evidence_snippet=" ".join(text.split())[:1000]))
            continue
        if not item.emit_signal:
            continue
        # Homepages are baselines. Undated article pages and material non-homepage
        # changes can still be developments; ordinary undated snapshots cannot.
        if source.source_type == "website":
            if is_homepage:
                continue
            if item.published_at is None and not item.is_article and not material_change:
                continue
            if prior and item.published_at is None and not material_change:
                continue
        signal_type, classifier_method = classify_with_model_details(item.title, text, source.category)
        analysis = _analyze_development(session, company_id, source, item.title, text, signal_type, item.published_at)
        event = Development(
            company_id=company_id, document_id=doc.id, what_happened=item.title,
            business_implication=str(analysis["interpretation"]),
            signal_type=signal_type,
            # A single source is not independent corroboration. Keep this false unless a human verifies it.
            verified=False, verification_status="not-reviewed",
            factual_claim=str(analysis["factual_claim"]), interpretation=str(analysis["interpretation"]),
            affected_business_area=str(analysis["affected_business_area"]), recommended_action=str(analysis["recommended_action"]),
            analysis_confidence=float(analysis["analysis_confidence"]), confidence_rationale=str(analysis["confidence_rationale"]),
            analysis_method=str(analysis["analysis_method"]), signal_classification_method=classifier_method,
        )
        session.add(event)
        session.flush()
        scored, reason = score_signal(
            title=item.title, content=text, profile_text=profile, category=source.category,
            reliability=source.reliability, published_at=item.published_at, discovered_at=doc.discovered_at, novelty=1.0,
            weights=load_ranking_weights(session, company_id),
        )
        signal = Signal(
            company_id=company_id, development_id=event.id, category=source.category,
            relevance=scored["relevance"], impact=scored["impact"], urgency=scored["urgency"],
            source_reliability=scored["source_reliability"], evidence_strength=scored["evidence_strength"],
            novelty=scored["novelty"], recency=scored["recency"], score=scored["score"], score_reason=reason,
        )
        session.add(signal)
        session.flush()
        snippet = " ".join(text.split())[:1000]
        session.add(SignalEvidence(signal_id=signal.id, document_id=doc.id, evidence_snippet=snippet))
        signal_count += 1
    if new_count and source.category == "competitors":
        # Names are suggestions only if the collected text explicitly compares them.
        from signal_stack.services.competitor_discovery import discover_competitors_from_documents
        discover_competitors_from_documents(session, company_id)
    return new_count, signal_count


def crawl_source(session: Session, company_id: int, source_id: int, *, allow_candidate: bool = False) -> CrawlRun:
    source = session.scalar(select(Source).where(Source.id == source_id, Source.company_id == company_id))
    if not source:
        raise ValueError("Source not found for this company")
    if source.source_type == "internal":
        raise ValueError("Internal upload sources are processed through the upload workflow")
    if source.status != "approved" and not allow_candidate:
        raise ValueError("Only approved sources can be crawled")
    run = CrawlRun(company_id=company_id, source_id=source.id)
    session.add(run)
    attempted_at = utcnow()
    company = session.get(Company, company_id)
    refresh_hours = company.source_refresh_hours if company else 24
    source.last_attempted_at = attempted_at
    source.last_checked_at = attempted_at
    try:
        result = fetch_public_url(source.url)
        docs = extract_documents(result, source.source_type)
        found, _ = _persist_documents(session, company_id, source, docs)
        run.documents_found = found
        run.status = "succeeded"
        source.last_status = "healthy"
        source.last_error = None
        source.last_success_at = utcnow()
        source.refresh_failure_count = 0
        source.next_due_at = source.last_success_at + timedelta(hours=refresh_hours)
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)[:2000]
        source.last_status = "failed"
        source.last_error = str(exc)[:2000]
        source.refresh_failure_count = min(30, source.refresh_failure_count + 1)
        retry_hours = min(24, refresh_hours, 2 ** min(source.refresh_failure_count - 1, 5))
        source.next_due_at = utcnow() + timedelta(hours=retry_hours)
    source.refresh_claimed_until = None
    run.ended_at = utcnow()
    session.commit()
    return run


def crawl_approved(session: Session, company_id: int, *, force: bool = False) -> dict[str, int]:
    from signal_stack.models import Company
    from datetime import timedelta

    company = session.get(Company, company_id)
    refresh_hours = company.source_refresh_hours if company else 24
    sources = session.scalars(select(Source).where(Source.company_id == company_id, Source.status == "approved").order_by(Source.id)).all()
    totals = {"sources": 0, "succeeded": 0, "failed": 0, "documents": 0}
    for source in sources:
        if source.source_type == "internal":
            continue
        due_at = source.next_due_at or (source.last_checked_at + timedelta(hours=refresh_hours) if source.last_checked_at else None)
        if not force and due_at and due_at > utcnow():
            continue
        run = crawl_source(session, company_id, source.id)
        totals["sources"] += 1
        totals["succeeded" if run.status == "succeeded" else "failed"] += 1
        totals["documents"] += run.documents_found
    return totals


def recent_signals(session: Session, company_id: int, *, days: int = 30, category: str | None = None, limit: int = 200, order: str = "newest") -> list[Signal]:
    cutoff = utcnow() - timedelta(days=days)
    query = select(Signal).where(Signal.company_id == company_id, Signal.created_at >= cutoff, Signal.status == "active")
    if category and category != "all":
        query = query.where(Signal.category == category)
    order_by = {
        "newest": (Signal.created_at.desc(), Signal.id.desc()),
        "oldest": (Signal.created_at.asc(), Signal.id.asc()),
        "score": (Signal.score.desc(), Signal.created_at.desc(), Signal.id.desc()),
    }
    if order not in order_by:
        raise ValueError("Signal order must be newest, oldest, or score")
    return list(session.scalars(query.order_by(*order_by[order]).limit(limit)).all())

