"""Resumable onboarding orchestration for a company."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_stack.models import Company, Document, OnboardingStage, utcnow
from signal_stack.services.company_research import profile_values, research_company
from signal_stack.services.classification import classify_company_context, profile_fingerprint
from signal_stack.services.competitor_discovery import discover_competitors_from_documents
from signal_stack.services.interview import start_interview
from signal_stack.services.source_discovery import discover_sources


def run_onboarding(session: Session, company_id: int) -> dict[str, OnboardingStage]:
    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Company not found")
    operations = {
        "research": lambda: research_company(session, company),
        "classification": lambda: classify_company_context(session, company_id),
        "source_discovery": lambda: discover_sources(session, company_id),
        "competitor_discovery": lambda: discover_competitors_from_documents(session, company_id),
        "interview": lambda: start_interview(session, company_id),
    }
    results: dict[str, OnboardingStage] = {}
    for name, operation in operations.items():
        if name == "research":
            fingerprint = company.website
        elif name in {"classification", "source_discovery", "interview"}:
            fingerprint = profile_fingerprint(session, company_id)
        else:
            documents = session.scalars(select(Document).where(Document.company_id == company_id).order_by(Document.id)).all()
            fingerprint = profile_fingerprint(session, company_id) + ":" + ":".join(document.content_hash for document in documents)
        stage = session.scalar(select(OnboardingStage).where(OnboardingStage.company_id == company_id, OnboardingStage.stage == name))
        if stage and stage.status == "succeeded" and stage.input_fingerprint == fingerprint:
            results[name] = stage
            continue
        if stage is None:
            stage = OnboardingStage(company_id=company_id, stage=name)
            session.add(stage)
        stage.status = "running"
        stage.attempts = (stage.attempts or 0) + 1
        stage.started_at = utcnow()
        stage.completed_at = None
        stage.error = None
        session.commit()
        try:
            output = operation()
            profile = profile_values(session, company_id)
            context_available = any(profile.get(key, "").strip() for key in ("company_description", "offerings_observed", "industry", "customer_segments", "customer_problem"))
            waiting_for_evidence = (name == "competitor_discovery" and not output) or (name == "source_discovery" and not context_available)
            stage.status = "awaiting-evidence" if waiting_for_evidence else "succeeded"
            stage.output_summary = f"Completed; {len(output) if hasattr(output, '__len__') else 1} result(s)."
            if name == "source_discovery" and not context_available:
                stage.output_summary += " Candidates are generic baselines; add first-party or founder context before treating discovery as complete."
            elif name == "competitor_discovery" and not output:
                stage.output_summary += " No collected comparison evidence is available yet."
        except Exception as exc:
            session.rollback()
            stage = session.scalar(select(OnboardingStage).where(OnboardingStage.company_id == company_id, OnboardingStage.stage == name))
            stage.status = "failed"
            stage.error = str(exc)[:2000]
            stage.output_summary = None
        stage.completed_at = utcnow()
        stage.input_fingerprint = fingerprint
        session.commit()
        results[name] = stage
    return results
