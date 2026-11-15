"""Evidence-grounded company context classification with conservative unknowns."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_stack.models import Company, CompanyClassification, ProfileAttribute, utcnow


# These are generic sector terms, never company-name rules. Only profile evidence is examined.
_INDUSTRIES = (
    ("climate technology", ("carbon accounting", "renewable energy", "emissions reporting", "decarbonization", "climate software")),
    ("healthcare", ("healthcare", "clinical", "hospital", "patient", "medical", "telemedicine", "diagnostic")),
    ("education technology", ("education", "learning", "student", "university", "school", "exam preparation", "online courses")),
    ("financial services", ("brokerage", "securities", "investment", "trading", "banking", "financial services", "payments", "equity", "order routing")),
    ("food services", ("restaurant", "food delivery", "grocery orders", "dining", "food marketplace", "kitchen")),
    ("logistics", ("logistics", "freight", "shipping", "warehouse", "last-mile delivery", "supply chain")),
    ("agriculture", ("agriculture", "farming", "crop", "livestock", "farmers", "agritech")),
    ("cybersecurity", ("cybersecurity", "threat detection", "identity security", "security monitoring")),
    ("enterprise software", ("enterprise software", "workflow software", "business software", "cloud software", "saas")),
    ("manufacturing", ("manufacturing", "factory", "industrial equipment", "production line")),
    ("travel and hospitality", ("travel booking", "hotel", "hospitality", "airline", "tourism")),
)

_SUBINDUSTRIES = {
    "climate technology": (("carbon accounting", "carbon accounting"), ("renewable energy", "renewable energy software")),
    "healthcare": (("telemedicine", "telemedicine"), ("diagnostic", "diagnostics"), ("hospital", "hospital services")),
    "education technology": (("exam preparation", "test preparation"), ("online learning", "online learning"), ("university", "higher education")),
    "financial services": (("brokerage", "brokerage"), ("payments", "payments"), ("securities", "securities trading")),
    "food services": (("food delivery", "food delivery"), ("restaurant", "restaurant marketplace"), ("grocery", "online grocery")),
}

_MODEL_TERMS = (
    (("marketplace", "platform connects", "buyers and sellers"), "marketplace"),
    (("subscription", "recurring subscription", "saas"), "subscription software"),
    (("transaction fee", "commission", "per transaction"), "transaction fees"),
    (("advertising", "ad-supported"), "advertising"),
    (("license", "licensing"), "software licensing"),
)


def _clean(value: str | None) -> str | None:
    value = " ".join((value or "").split())
    return value[:1200] or None


def _field(rows: dict[str, ProfileAttribute], *keys: str) -> tuple[list[str], list[dict[str, str]]]:
    values: list[str] = []
    evidence: list[dict[str, str]] = []
    for key in keys:
        row = rows.get(key)
        value = _clean(row.value) if row else None
        if not row or not value:
            continue
        if value not in values:
            values.append(value)
        evidence.append({
            "field": key, "status": row.status, "url": row.evidence_url or "",
            "snippet": (row.evidence_snippet or value)[:500], "value": value,
        })
    return values, evidence


def profile_fingerprint(session: Session, company_id: int) -> str:
    from signal_stack.config import settings

    rows = session.scalars(select(ProfileAttribute).where(ProfileAttribute.company_id == company_id)).all()
    fingerprint_input = [
        {"key": row.key, "value": row.value, "status": row.status, "url": row.evidence_url, "snippet": row.evidence_snippet}
        for row in sorted(rows, key=lambda item: item.key)
    ]
    fingerprint_input.append({"provider": settings.llm_provider, "model": settings.ollama_model if settings.llm_provider == "ollama" else None})
    return hashlib.sha256(json.dumps(fingerprint_input, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _validated_local_suggestion(rows: dict[str, ProfileAttribute]) -> tuple[dict[str, str | None], list[dict[str, str]]]:
    from signal_stack.providers.llm.ollama import classify_company_profile

    profile = {key: row.value[:1200] for key, row in rows.items() if row.value.strip()}
    payload = classify_company_profile(profile)
    keys = {"industry", "sub_industry", "business_model"}
    if set(payload) != keys | {"evidence"}:
        raise ValueError("Ollama classification keys did not match the structured schema")
    values: dict[str, str | None] = {}
    for key in keys:
        value = payload[key]
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value.strip()) > 160):
            raise ValueError("Ollama returned an invalid classification label")
        values[key] = value.strip() if isinstance(value, str) else None
    refs = payload["evidence"]
    if not isinstance(refs, list):
        raise ValueError("Ollama evidence must be a list")
    verified_refs: list[dict[str, str]] = []
    supported: set[str] = set()
    for item in refs:
        if not isinstance(item, dict) or set(item) != {"classification_field", "profile_key", "quote"}:
            raise ValueError("Ollama evidence reference did not match the structured schema")
        field, profile_key, quote = item["classification_field"], item["profile_key"], item["quote"]
        row = rows.get(profile_key) if isinstance(profile_key, str) else None
        if field not in keys or not isinstance(quote, str) or not quote.strip() or not row:
            raise ValueError("Ollama returned an unsupported evidence reference")
        if quote.casefold() not in (row.value + " " + (row.evidence_snippet or "")).casefold():
            raise ValueError("Ollama evidence quote was not found in the company profile")
        supported.add(field)
        verified_refs.append({
            "field": f"classification:{field}", "profile_field": profile_key,
            "status": row.status, "url": row.evidence_url or "", "snippet": quote[:500],
        })
    if any(values[field] and field not in supported for field in keys):
        raise ValueError("Ollama classification omitted supporting profile evidence")
    return values, verified_refs


def classify_company_context(session: Session, company_id: int, *, use_llm: bool | None = None) -> CompanyClassification:
    from signal_stack.config import settings

    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Company not found")
    use_llm = settings.llm_provider == "ollama" if use_llm is None else use_llm
    rows = {row.key: row for row in session.scalars(select(ProfileAttribute).where(ProfileAttribute.company_id == company_id)).all()}
    fingerprint = profile_fingerprint(session, company_id) + (":ollama" if use_llm else ":deterministic")
    latest = session.scalar(select(CompanyClassification).where(CompanyClassification.company_id == company_id).order_by(CompanyClassification.created_at.desc(), CompanyClassification.id.desc()))
    if latest and latest.input_fingerprint == fingerprint:
        return latest

    grounded_fields = {"company_description", "offerings_observed", "products_services", "customer_segments", "customer_problem", "geography", "business_model", "industry", "sub_industry", "regulatory_environment", "business_drivers", "interview_customer_needs", "interview_growth", "interview_decisions"}
    evidence = [
        {"field": row.key, "status": row.status, "url": row.evidence_url or "", "snippet": (row.evidence_snippet or row.value)[:500], "value": row.value[:1200]}
        for row in rows.values() if row.key in grounded_fields and row.value.strip()
    ]
    text = " ".join(item["value"] for item in evidence).casefold()
    explicit_industry, explicit_evidence = _field(rows, "industry")
    if explicit_industry:
        industry = explicit_industry[0]
        matched_terms = [industry]
        evidence.extend(item for item in explicit_evidence if item not in evidence)
        confidence = .85
    else:
        matches = [(name, [term for term in terms if term in text]) for name, terms in _INDUSTRIES]
        name, terms = max(matches, key=lambda item: len(item[1]), default=(None, []))
        industry = name if len(terms) else None
        matched_terms = terms
        confidence = min(.78, .58 + .08 * (len(terms) - 1)) if industry else 0.0

    provider = "deterministic-profile"
    fallback_note = None
    local_suggestions: dict[str, str | None] = {}
    local_evidence: list[dict[str, str]] = []
    if use_llm:
        try:
            local_suggestions, local_evidence = _validated_local_suggestion(rows)
            evidence.extend(item for item in local_evidence if item not in evidence)
            if local_evidence:
                provider = "ollama-local"
        except Exception as exc:
            provider = "deterministic-fallback"
            fallback_note = f"Local classification unavailable or invalid ({type(exc).__name__}); deterministic fallback used."

    explicit_subindustry, sub_evidence = _field(rows, "sub_industry")
    sub_industry = explicit_subindustry[0] if explicit_subindustry else None
    if not sub_industry and industry:
        sub_industry = next((label for needle, label in _SUBINDUSTRIES.get(industry, ()) if needle in text), None)

    explicit_model, model_evidence = _field(rows, "business_model")
    business_model = explicit_model[0] if explicit_model else None
    if not business_model:
        business_model = next((label for needles, label in _MODEL_TERMS if any(needle in text for needle in needles)), None)
    if provider == "ollama-local":
        if not explicit_industry and local_suggestions.get("industry"):
            industry = local_suggestions["industry"]
        if not explicit_subindustry and local_suggestions.get("sub_industry"):
            sub_industry = local_suggestions["sub_industry"]
        if not explicit_model and local_suggestions.get("business_model"):
            business_model = local_suggestions["business_model"]
        confidence = min(.72, .55 + .04 * len(local_evidence))

    products, product_evidence = _field(rows, "products_services", "offerings_observed")
    segments, segment_evidence = _field(rows, "customer_segments", "interview_segments")
    geography, geography_evidence = _field(rows, "geography", "interview_geography")
    problems, problem_evidence = _field(rows, "customer_problem", "interview_customer_needs")
    drivers, driver_evidence = _field(rows, "business_drivers", "interview_growth", "interview_decisions")
    regulations, regulatory_evidence = _field(rows, "regulatory_environment", "interview_regulatory_environment")
    for group in (sub_evidence, model_evidence, product_evidence, segment_evidence, geography_evidence, problem_evidence, driver_evidence, regulatory_evidence):
        evidence.extend(item for item in group if item not in evidence)
    uncertainty = []
    if fallback_note:
        uncertainty.append(fallback_note)
    if not industry:
        uncertainty.append("industry")
    if not sub_industry:
        uncertainty.append("sub_industry")
    if not business_model:
        uncertainty.append("business_model")
    for field_name, values in (("products_services", products), ("customer_segments", segments), ("geography", geography), ("customer_problems", problems), ("business_drivers", drivers), ("regulatory_environment", regulations)):
        if not values:
            uncertainty.append(field_name)

    result = CompanyClassification(
        company_id=company_id, industry=industry, sub_industry=sub_industry, business_model=business_model,
        products_services_json=json.dumps(products, ensure_ascii=False), customer_segments_json=json.dumps(segments, ensure_ascii=False),
        geography_json=json.dumps(geography, ensure_ascii=False), customer_problems_json=json.dumps(problems, ensure_ascii=False),
        business_drivers_json=json.dumps(drivers, ensure_ascii=False), regulatory_environment_json=json.dumps(regulations, ensure_ascii=False),
        evidence_json=json.dumps(evidence, ensure_ascii=False), confidence=confidence,
        uncertainty=json.dumps(uncertainty), provider=provider, input_fingerprint=fingerprint, created_at=utcnow(),
    )
    session.add(result)
    session.flush()
    return result
