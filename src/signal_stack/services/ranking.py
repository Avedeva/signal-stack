from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_stack.models import CompanySetting, Signal


DEFAULT_RANKING_WEIGHTS = {
    "relevance": 0.25,
    "impact": 0.22,
    "urgency": 0.15,
    "source_reliability": 0.14,
    "evidence_strength": 0.12,
    "recency": 0.08,
    "novelty": 0.04,
}
STOP_WORDS = {"about", "after", "again", "also", "among", "because", "could", "from", "have", "into", "more", "most", "other", "over", "such", "than", "that", "their", "there", "these", "they", "this", "through", "under", "were", "where", "which", "while", "with", "your", "company", "business", "customers"}


def load_ranking_weights(session: Session, company_id: int) -> dict[str, float]:
    row = session.scalar(select(CompanySetting).where(CompanySetting.company_id == company_id, CompanySetting.key == "ranking_weights"))
    if row:
        try:
            values = json.loads(row.value)
            if set(values) == set(DEFAULT_RANKING_WEIGHTS) and all(isinstance(value, (int, float)) and 0 <= value <= 1 for value in values.values()):
                total = sum(values.values())
                if total > 0:
                    return {key: float(values[key]) / total for key in DEFAULT_RANKING_WEIGHTS}
        except (ValueError, TypeError):
            pass
    return dict(DEFAULT_RANKING_WEIGHTS)


def save_ranking_weights(session: Session, company_id: int, values: dict[str, float]) -> dict[str, float]:
    if set(values) != set(DEFAULT_RANKING_WEIGHTS):
        raise ValueError("Ranking weights must include relevance, impact, urgency, source reliability, evidence strength, recency, and novelty")
    if any(not isinstance(value, (int, float)) or value < 0 or value > 1 for value in values.values()):
        raise ValueError("Each ranking weight must be between 0 and 1")
    total = sum(values.values())
    if total <= 0:
        raise ValueError("At least one ranking weight must be greater than zero")
    normalized = {key: float(values[key]) / total for key in DEFAULT_RANKING_WEIGHTS}
    row = session.scalar(select(CompanySetting).where(CompanySetting.company_id == company_id, CompanySetting.key == "ranking_weights"))
    if row is None:
        row = CompanySetting(company_id=company_id, key="ranking_weights", value=json.dumps(normalized, sort_keys=True))
        session.add(row)
    else:
        row.value = json.dumps(normalized, sort_keys=True)
    # Scores are the normalized weighted sum of the existing seven dimensions.
    # Recompute within the caller's transaction; evidence and classifications remain untouched.
    for signal in session.scalars(select(Signal).where(Signal.company_id == company_id, Signal.status == "active").order_by(Signal.id)).all():
        components = {
            "relevance": signal.relevance,
            "impact": signal.impact,
            "urgency": signal.urgency,
            "source_reliability": signal.source_reliability,
            "evidence_strength": signal.evidence_strength,
            "recency": signal.recency,
            "novelty": signal.novelty,
        }
        dimensions = {key: min(1.0, max(0.0, float(value or 0.0))) for key, value in components.items()}
        signal.score = round(sum(dimensions[key] * normalized[key] for key in DEFAULT_RANKING_WEIGHTS), 4)
        breakdown = ", ".join(f"{key} {dimensions[key]:.2f}×{normalized[key]:.2f}" for key in DEFAULT_RANKING_WEIGHTS)
        signal.score_reason = f"Weighted score {signal.score:.2f}; {breakdown}. Recalculated from stored dimensions after ranking weights changed; evidence and classification were not changed."
    return normalized


def _tokens(value: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", value) if word.lower() not in STOP_WORDS}


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def score_signal(
    *, title: str, content: str, profile_text: str, category: str, reliability: float, published_at,
    discovered_at, novelty: float, weights: dict[str, float] | None = None,
) -> tuple[dict[str, float], str]:
    doc_tokens = _tokens(f"{title} {content[:3000]}")
    profile_tokens = _tokens(profile_text)
    overlap = len(doc_tokens & profile_tokens)
    relevance = min(1.0, 0.2 + 0.8 * overlap / max(1, min(len(doc_tokens), 12)))
    impact = 0.45
    if re.search(r"\b(?:million|billion|national|ban|rule|regulat|launch|acquir|funding|expan|investigat|penalt|shutdown)\w*\b", title + " " + content[:500], re.I):
        impact = 0.78
    if category == "internal":
        impact = max(impact, 0.6)
    urgency = 0.4
    if re.search(r"\b(?:immediate|deadline|effective|today|urgent|suspend|ban|recall|breach|penalt)\w*\b", title + " " + content[:300], re.I):
        urgency = 0.85
    # Unknown publication dates stay unknown; use a modest neutral recency prior.
    recency = 0.25
    recency_note = ""
    if published_at:
        observed = _utc_naive(discovered_at)
        published = _utc_naive(published_at)
        if published > observed + timedelta(hours=1):
            recency_note = " A future publication timestamp was capped because its timing is uncertain."
        else:
            age = max(0, (observed - published).days)
            recency = max(0.1, 1.0 - age / 365)
    evidence_strength = min(1.0, 0.32 + min(len(content.split()), 180) / 260 + (0.15 if title else 0))
    reliability = min(1.0, max(0.0, reliability))
    novelty = min(1.0, max(0.0, novelty))
    score_weights = weights or DEFAULT_RANKING_WEIGHTS
    total = sum(max(0.0, float(score_weights.get(key, 0))) for key in DEFAULT_RANKING_WEIGHTS)
    if total <= 0:
        score_weights = DEFAULT_RANKING_WEIGHTS
        total = 1.0
    score_weights = {key: max(0.0, float(score_weights.get(key, 0))) / total for key in DEFAULT_RANKING_WEIGHTS}
    values = {"relevance": relevance, "impact": impact, "urgency": urgency, "source_reliability": reliability, "evidence_strength": evidence_strength, "recency": recency, "novelty": novelty}
    score = round(sum(values[key] * score_weights[key] for key in DEFAULT_RANKING_WEIGHTS), 4)
    breakdown = ", ".join(f"{key} {values[key]:.2f}×{score_weights[key]:.2f}" for key in DEFAULT_RANKING_WEIGHTS)
    return {**values, "score": score}, f"Weighted score {score:.2f}; {breakdown}. Scores prioritize triage; they do not verify the event.{recency_note}"

