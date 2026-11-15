"""Small, deterministic analysis stages over source-attributed evidence."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvidenceRecord:
    source_title: str
    source_url: str
    category: str
    title: str
    content: str
    published_at: object | None = None


@dataclass(frozen=True)
class AnalysisContext:
    company_name: str
    profile_key: str | None = None
    profile_value: str | None = None
    profile_status: str | None = None
    profile_evidence_url: str | None = None
    industry: str | None = None
    industry_provider: str | None = None
    industry_confidence: float | None = None


@dataclass(frozen=True)
class EventFacts:
    title: str
    source_title: str
    source_url: str
    category: str
    excerpt: str
    published_at: object | None


@dataclass(frozen=True)
class DevelopmentAnalysis:
    factual_claim: str
    interpretation: str
    affected_business_area: str
    recommended_action: str
    analysis_confidence: float
    confidence_rationale: str
    analysis_method: str = "deterministic-profile-v2"


_AREA_BY_CATEGORY = {
    "customers": "customer acquisition, experience, and retention",
    "competitors": "product positioning and customer alternatives",
    "market": "market exposure and strategic planning",
    "internal": "operations and customer feedback",
}


def extract_event(evidence: EvidenceRecord) -> EventFacts:
    """Capture the reported headline, excerpt and provenance without embellishment."""
    title = " ".join(evidence.title.split())[:1000]
    excerpt = " ".join(evidence.content.split())[:360]
    return EventFacts(
        title=title,
        source_title=" ".join(evidence.source_title.split())[:500],
        source_url=evidence.source_url,
        category=evidence.category,
        excerpt=excerpt,
        published_at=evidence.published_at,
    )


def assess_company_impact(event: EventFacts, context: AnalysisContext, signal_type: str) -> tuple[str, str]:
    """Describe a bounded implication; the source's claim is not treated as confirmed."""
    profile_value = " ".join((context.profile_value or "").split())[:180]
    profile_key = (context.profile_key or "company context").replace("_", " ")
    area_base = _AREA_BY_CATEGORY.get(event.category, "leadership priorities")
    if not profile_value:
        interpretation = (
            f"The source reports ‘{event.title}’, which falls within the {event.category} monitoring area. "
            "Company-specific fit is uncertain because no relevant profile context is recorded. "
            "The underlying event is not independently verified."
        )
        return interpretation, area_base

    possible_effect = {
        "threat": "could affect",
        "opportunity": "could create an opening relevant to",
        "neutral": "may be relevant to",
    }.get(signal_type, "may be relevant to")
    industry = f" The profile classification lists {context.industry}; this is contextual metadata, not verification." if context.industry else ""
    interpretation = (
        f"Because the company profile records {profile_key} as ‘{profile_value}’, this reported development {possible_effect} "
        f"that context. The match is a hypothesis for leadership review, not a measured revenue or customer impact. "
        f"The source's report is not independently verified.{industry}"
    )
    return interpretation, f"{area_base} — {profile_value[:120]}"


def recommend_action(event: EventFacts, context: AnalysisContext, signal_type: str) -> str:
    profile_value = " ".join((context.profile_value or "").split())[:160]
    if not profile_value:
        return (
            "No company-specific action can be justified without relevant profile context. "
            "Review the cited source and record any affected product, customer segment, or operating priority before deciding."
        )
    key = (context.profile_key or "business priority").replace("_", " ")
    basis = f"the recorded {key} ‘{profile_value}’"
    if signal_type == "threat":
        return (
            f"Ask the owner of {basis} to check the cited source and identify one observable exposure indicator; "
            "escalate only if company evidence confirms a material effect."
        )
    if signal_type == "opportunity":
        return (
            f"Ask the owner of {basis} to compare the reported change with current customer or product evidence "
            "and define a small, measurable test before committing resources."
        )
    return (
        f"Have the owner of {basis} review the original source and decide whether the development changes an existing "
        "priority; revisit only if follow-up evidence appears."
    )


def calibrate_interpretation_confidence(event: EventFacts, context: AnalysisContext) -> tuple[float, str]:
    """Score contextual fit conservatively; this number is explicitly not fact verification."""
    profile_value = bool((context.profile_value or "").strip())
    confidence = min(
        0.5,
        0.25
        + (0.1 if len(event.excerpt.split()) >= 12 else 0)
        + (0.1 if profile_value else 0)
        + (0.05 if profile_value and context.profile_evidence_url else 0),
    )
    date_basis = "publication date is recorded" if event.published_at else "publication date is unknown"
    profile_basis = (
        f"Relevant profile field {context.profile_key} has status {context.profile_status or 'unknown'}"
        if profile_value
        else "No relevant company profile field was available"
    )
    source = event.source_title or "the original source"
    rationale = (
        f"Confidence describes interpretation fit, not a truth or verification score. It uses the excerpt and {source}; "
        f"{date_basis}. {profile_basis}. One source is available and independent corroboration is absent."
    )
    if context.industry:
        provider = context.industry_provider or "unknown provider"
        rationale += (
            f" Company classification ({context.industry}, {provider}, confidence "
            f"{float(context.industry_confidence or 0):.2f}) is contextual only, not fact verification."
        )
    return confidence, rationale


def analyze_evidence(evidence: EvidenceRecord, context: AnalysisContext, signal_type: str) -> DevelopmentAnalysis:
    """Run event extraction, company-impact analysis, recommendations and fit calibration."""
    event = extract_event(evidence)
    interpretation, area = assess_company_impact(event, context, signal_type)
    action = recommend_action(event, context, signal_type)
    confidence, rationale = calibrate_interpretation_confidence(event, context)
    claim = f"Source text reports: {event.title}. Supporting excerpt: {event.excerpt}. Original source URL: {event.source_url}."
    return DevelopmentAnalysis(
        factual_claim=claim,
        interpretation=interpretation,
        affected_business_area=area,
        recommended_action=action,
        analysis_confidence=confidence,
        confidence_rationale=rationale,
    )
