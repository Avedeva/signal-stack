from __future__ import annotations

import json
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_stack.models import Development, Document, Source, VerificationHistory
from signal_stack.services.security import normalize_url


VERIFICATION_STATES = {"not-reviewed", "source-reviewed", "independently-corroborated", "disputed"}
_ALLOWED_TRANSITIONS = {
    "not-reviewed": {"source-reviewed", "disputed"},
    "source-reviewed": {"independently-corroborated", "disputed"},
    "independently-corroborated": {"disputed"},
    "disputed": {"source-reviewed", "independently-corroborated"},
}


def _domain(url: str) -> str:
    try:
        return (urlsplit(normalize_url(url)).hostname or "").casefold()
    except (ValueError, TypeError):
        return ""


def transition_verification(
    session: Session,
    company_id: int,
    development_id: int,
    new_status: str,
    *,
    reviewer_identity: str,
    explanation: str,
    evidence_urls: list[str] | None = None,
    evidence_source_ids: list[int] | None = None,
) -> VerificationHistory:
    if new_status not in VERIFICATION_STATES:
        raise ValueError("Unsupported verification state")
    reviewer = " ".join((reviewer_identity or "").split())[:240]
    if not reviewer:
        raise ValueError("Reviewer identity is required")
    note = " ".join((explanation or "").split())
    if len(note) < 10:
        raise ValueError("A meaningful explanation is required for each verification action")
    development = session.scalar(select(Development).where(Development.id == development_id, Development.company_id == company_id))
    if development is None:
        raise ValueError("Development not found for this company")
    previous = development.verification_status
    if new_status == previous:
        raise ValueError("Verification action cannot repeat the same state")
    if new_status not in _ALLOWED_TRANSITIONS.get(previous, set()):
        raise ValueError(f"Transition from {previous} to {new_status} is not allowed")

    document = session.scalar(select(Document).where(Document.id == development.document_id, Document.company_id == company_id))
    if document is None:
        raise ValueError("The development's source record is unavailable")
    urls = []
    for raw in evidence_urls or []:
        if not raw or not raw.strip():
            continue
        try:
            normalized = normalize_url(raw)
        except ValueError as exc:
            raise ValueError("Verification evidence URLs must be valid public HTTP(S) URLs") from exc
        if normalized not in urls:
            urls.append(normalized)

    source_ids = list(dict.fromkeys(int(value) for value in (evidence_source_ids or [])))
    source_rows = []
    if source_ids:
        source_rows = session.scalars(select(Source).where(Source.company_id == company_id, Source.id.in_(source_ids))).all()
        if len(source_rows) != len(source_ids):
            raise ValueError("Verification evidence source must belong to this company")

    original_domain = _domain(document.canonical_url)
    if new_status == "source-reviewed":
        original_url = _domain(document.canonical_url)
        if original_url:
            normalized_original = normalize_url(document.canonical_url)
            if normalized_original not in urls:
                urls.insert(0, normalized_original)
        if document.source_id and document.source_id not in source_ids:
            source_ids.append(document.source_id)
    if new_status == "independently-corroborated":
        independent_urls = [url for url in urls if _domain(url) and _domain(url) != original_domain]
        original_source = session.get(Source, document.source_id) if document.source_id else None
        independent_sources = [
            row for row in source_rows
            if row.source_type != "internal" and _domain(row.url)
            and (not original_source or _domain(row.url) != _domain(original_source.url))
        ]
        if not independent_urls and not independent_sources:
            raise ValueError("Independent corroboration requires evidence from a distinct source/domain")
    if new_status == "disputed" and len(note) < 20:
        raise ValueError("A dispute requires a specific explanation of at least 20 characters")

    development.verification_status = new_status
    development.verified = new_status == "independently-corroborated"
    history = VerificationHistory(
        company_id=company_id,
        development_id=development_id,
        previous_status=previous,
        new_status=new_status,
        reviewer_identity=reviewer,
        explanation=note,
        evidence_urls_json=json.dumps(urls, ensure_ascii=False),
        evidence_source_ids_json=json.dumps(source_ids),
    )
    session.add(history)
    session.flush()
    return history


def verification_history(session: Session, company_id: int, development_id: int) -> list[VerificationHistory]:
    return list(session.scalars(
        select(VerificationHistory)
        .where(VerificationHistory.company_id == company_id, VerificationHistory.development_id == development_id)
        .order_by(VerificationHistory.created_at.asc(), VerificationHistory.id.asc())
    ).all())
