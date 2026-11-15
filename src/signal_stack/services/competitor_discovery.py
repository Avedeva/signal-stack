from __future__ import annotations

import re
import json
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from signal_stack.models import Company, Competitor, Document, Source


_COMPARISON = re.compile(
    r"(?P<left>[A-Z][A-Za-z0-9&'-]*(?:\s+[A-Z][A-Za-z0-9&'-]*){0,2})"
    r"\s+(?:vs\.?|versus|compared with|compared to)\s+"
    r"(?P<right>[A-Z][A-Za-z0-9&'-]*(?:\s+[A-Z][A-Za-z0-9&'-]*){0,2})"
)
_EXPLICIT = re.compile(r"(?:alternatives to|competitors of|rivals of)\s+([^.;:!?]{2,100})", re.I)
_ENTITY = re.compile(r"\b[A-Z][A-Za-z0-9&'-]*(?:\s+[A-Z][A-Za-z0-9&'-]*){0,2}\b")
_TYPE_RULES = (
    ("substitute", re.compile(r"\b(?:substitute|alternative|instead of|replace|replacement for)\b", re.I)),
    ("emerging", re.compile(r"\b(?:new entrant|emerging competitor|recent entrant|launching into the category)\b", re.I)),
    ("indirect", re.compile(r"\b(?:indirect competitor|different approach|adjacent market)\b", re.I)),
    ("direct", re.compile(r"\b(?:direct competitor|competes directly|same customer segment|same customer use case|similar product)\b", re.I)),
)


def _candidate_names(text: str, company_name: str) -> set[str]:
    names: set[str] = set()
    for match in _COMPARISON.finditer(text):
        for candidate in (match.group("left"), match.group("right")):
            if candidate.casefold() != company_name.casefold() and company_name.casefold() not in candidate.casefold():
                names.add(candidate.strip(" .,:;!?"))
    for match in _EXPLICIT.finditer(text):
        for candidate in _ENTITY.findall(match.group(1)):
            if candidate.casefold() != company_name.casefold() and company_name.casefold() not in candidate.casefold():
                names.add(candidate.strip(" .,:;!?"))
    return {name for name in names if 2 < len(name) <= 80}


def _competitor_type(text: str) -> tuple[str, float]:
    for competitor_type, pattern in _TYPE_RULES:
        if pattern.search(text):
            confidence = {"direct": .72, "indirect": .62, "substitute": .68, "emerging": .58}[competitor_type]
            return competitor_type, confidence
    return "unclassified", .3


def _entity_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def _role_conflict(name: str, text: str) -> bool:
    role = r"(?:customer|client|supplier|vendor|investor|partner|shareholder|funder)s?"
    subject = re.escape(name)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        if name.casefold() not in sentence.casefold():
            continue
        if re.search(r"\bnot\s+(?:a|an|the\s+)?(?:competitor|alternative|rival)\b|\bdoes\s+not\s+compete\b", sentence, re.I):
            return True
        if re.search(rf"\b{subject}\b\s+(?:is|was|acts as|serves as)\s+(?:(?:a|an|the)\s+)?{role}\b", sentence, re.I):
            return True
        if re.search(rf"\b{role}\b(?:\s+(?:of|to|for|with))?\s+{subject}\b", sentence, re.I):
            return True
    return False


def update_competitor(
    session: Session, company_id: int, competitor_id: int, *, name: str,
    website: str | None, competitor_type: str, rationale: str,
    profile_fields: dict[str, str] | None = None,
    supporting_source_urls: list[str] | None = None,
) -> Competitor:
    from signal_stack.services.security import normalize_url

    row = session.scalar(select(Competitor).where(Competitor.id == competitor_id, Competitor.company_id == company_id))
    if not row:
        raise ValueError("Competitor not found for this company")
    clean_name = " ".join(name.split())
    if not clean_name:
        raise ValueError("Competitor name is required")
    if competitor_type not in {"direct", "indirect", "substitute", "emerging", "unclassified"}:
        raise ValueError("Unsupported competitor type")
    duplicate = session.scalar(select(Competitor.id).where(
        Competitor.company_id == company_id, func.lower(Competitor.name) == clean_name.casefold(), Competitor.id != competitor_id,
    ))
    if duplicate:
        raise ValueError("A competitor with this name already exists for the company")
    normalized_website = normalize_url(website) if website and website.strip() else None
    if normalized_website:
        new_host = (urlsplit(normalized_website).hostname or "").casefold().removeprefix("www.")
        for other in session.scalars(select(Competitor).where(Competitor.company_id == company_id, Competitor.id != competitor_id)).all():
            if not other.website:
                continue
            other_host = (urlsplit(other.website).hostname or "").casefold().removeprefix("www.")
            if new_host and new_host == other_host:
                raise ValueError("A competitor profile already uses the same website domain")
    allowed_profile_fields = {
        "products_services", "target_market", "customer_segments", "geographic_focus", "competitive_relationship",
    }
    supplied_profile_fields = profile_fields or {}
    unknown_fields = set(supplied_profile_fields) - allowed_profile_fields
    if unknown_fields:
        raise ValueError(f"Unsupported competitor profile field(s): {', '.join(sorted(unknown_fields))}")
    normalized_source_urls = None
    if supporting_source_urls is not None:
        if len(supporting_source_urls) > 10:
            raise ValueError("A competitor profile can cite at most 10 supporting sources")
        normalized_source_urls = list(dict.fromkeys(normalize_url(url) for url in supporting_source_urls if url.strip()))
    row.name = clean_name[:240]
    row.website = normalized_website
    row.competitor_type = competitor_type
    row.rationale = rationale.strip()[:2000] or "Manually edited; supporting rationale should be reviewed."
    for field, value in supplied_profile_fields.items():
        setattr(row, field, " ".join(str(value or "").split())[:2000])
    if normalized_source_urls is not None:
        row.supporting_source_urls_json = json.dumps(normalized_source_urls, ensure_ascii=False)
    session.commit()
    return row


def discover_competitors_from_documents(session: Session, company_id: int, *, limit: int = 200) -> list[Competitor]:
    """Extract only explicit comparisons; every result remains a cited, unverified candidate."""
    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Company not found")
    rows = session.execute(
        select(Document, Source)
        .join(Source, Document.source_id == Source.id)
        .where(Document.company_id == company_id, Source.company_id == company_id, Source.category == "competitors")
        .order_by(Document.discovered_at.desc()).limit(limit)
    ).all()
    added = []
    existing_names = session.scalars(select(Competitor.name).where(Competitor.company_id == company_id)).all()
    existing_keys = {_entity_key(name) for name in existing_names}
    for document, _source in rows:
        evidence = f"{document.title}. {document.content[:1200]}"
        for name in _candidate_names(evidence, company.name):
            key = _entity_key(name)
            if key in existing_keys or _role_conflict(name, evidence):
                continue
            candidate_type, confidence = _competitor_type(evidence)
            sentences = re.split(r"(?<=[.!?])\s+", evidence)
            snippet = next((sentence for sentence in sentences if name.casefold() in sentence.casefold()), evidence)[:700]
            rationale = (
                f"Unverified {candidate_type} candidate: a collected competitor-query result explicitly compared or described ‘{name}’ with alternatives. "
                f"Review the cited item and confirm the same customer problem before approval. Evidence title: {document.title[:500]}"
            )
            candidate = Competitor(
                company_id=company_id, name=name, competitor_type=candidate_type, discovery_confidence=confidence,
                rationale=rationale, evidence_url=document.canonical_url, evidence_snippet=snippet, status="candidate",
                competitive_relationship="Unverified explicit comparison found in collected evidence; reviewer confirmation is required.",
                supporting_source_urls_json=json.dumps([document.canonical_url]), discovered_at=document.discovered_at,
            )
            session.add(candidate)
            added.append(candidate)
            existing_keys.add(key)
    if added:
        added.sort(key=lambda item: (-item.discovery_confidence, item.name.casefold()))
        session.commit()
    return added

