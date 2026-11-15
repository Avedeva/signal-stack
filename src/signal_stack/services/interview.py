from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_stack.models import Company, InterviewQuestion, InterviewSession, utcnow
from signal_stack.services.classification import classify_company_context, profile_fingerprint
from signal_stack.services.company_research import profile_values, save_profile_value, save_profile_version


def _meaningful_follow_up(topic: str, answer: str) -> str:
    subject = " ".join(answer.split()).strip(" .,:;!?-")[:100]
    quoted_subject = f"‘{subject}’" if subject else "that answer"
    templates = {
        "revenue": f"For {quoted_subject}, which product and customer segment drive the revenue, and what measure tracks its economics?",
        "segments": f"What is one concrete example involving {quoted_subject}, and what retention, conversion, or usage evidence shows this segment's value?",
        "customer_needs": f"What is one concrete example of {quoted_subject} from a customer, and how often does it occur?",
        "differentiation": f"What is one concrete example of a customer choosing you for {quoted_subject}, and what evidence records that choice?",
        "alternatives": f"What is one concrete example involving {quoted_subject}, and which specific customer need does that alternative serve?",
        "market_change": f"For {quoted_subject}, what observable indicator would show this change is affecting your business?",
        "risks": f"For {quoted_subject}, what early warning sign should leadership monitor and who owns the response?",
        "growth": f"For {quoted_subject}, what measurable milestone would show progress in the next quarter?",
        "internal": f"For {quoted_subject}, which non-confidential record can show the pattern and how frequently is it updated?",
        "decisions": f"For {quoted_subject}, what evidence would change the decision and who should review it?",
    }
    return templates.get(topic, f"What is one concrete example behind {quoted_subject}, and what evidence supports it?")


def _context(profile: dict[str, str], key: str, fallback: str) -> str:
    value = profile.get(key, "").strip()
    if value:
        return value[:180].rstrip(".,; ")
    return fallback


def build_questions(profile: dict[str, str], company_name: str) -> list[tuple[str, str]]:
    """Create a ten-question interview from observed business details and gaps."""
    offering = _context(profile, "offerings_observed", "your products or services")
    audience = _context(profile, "customer_segments", "the customers you serve")
    problem = _context(profile, "customer_problem", "the customer problem your business addresses")
    positioning = _context(profile, "company_description", "your value proposition")
    alternatives = _context(profile, "competitors", "alternatives customers consider")
    industry = _context(profile, "industry", "your market")
    geography = _context(profile, "geography", "your priority geographies")
    return [
        ("revenue", f"For {company_name}, which parts of {offering} currently generate revenue, and how do their economics differ?"),
        ("segments", f"Which customer segments within {audience} have the highest value or strongest retention, and what evidence supports that?"),
        ("customer_needs", f"What unmet needs or repeated frustrations do you hear around {problem}, and where do customers express them in their own words?"),
        ("differentiation", f"Why do customers choose {company_name} over other options, given the positioning we found ({positioning})?"),
        ("alternatives", f"Which named competitors or substitutes do buyers compare with {company_name}, including alternatives mentioned as {alternatives}?"),
        ("market_change", f"Which changes in {industry} or {geography} could materially alter demand, costs, or how customers decide?"),
        ("risks", "What external event would most threaten your plan over the next year, and what early signal should we monitor?"),
        ("growth", "What are the two most important growth outcomes for the next 6–12 months, and which customer or product is central to them?"),
        ("internal", "Which internal records can we use to spot objections, lost deals, support themes, product requests, or operational risks? Skip anything confidential."),
        ("decisions", "Which recurring leadership decisions should the Monday brief help you make, and what evidence would change your decision?"),
    ]


def _validate_questions(questions: list[tuple[str, str]]) -> list[tuple[str, str]]:
    from signal_stack.providers.llm.ollama import REQUIRED_TOPICS

    if not isinstance(questions, list) or len(questions) != 10:
        raise ValueError("Interview output must contain exactly ten core questions")
    cleaned: list[tuple[str, str]] = []
    for expected, item in zip(REQUIRED_TOPICS, questions):
        if not isinstance(item, (tuple, list)) or len(item) != 2 or item[0] != expected or not isinstance(item[1], str):
            raise ValueError("Interview output topics do not match the required ten-question structure")
        question = " ".join(item[1].split())
        if not 20 <= len(question) <= 600 or question.casefold() in {prior.casefold() for _, prior in cleaned}:
            raise ValueError("Interview output contains an invalid or duplicate question")
        cleaned.append((expected, question))
    return cleaned


def start_interview(session: Session, company_id: int, *, use_llm: bool | None = None) -> InterviewSession:
    active = session.scalar(select(InterviewSession).where(InterviewSession.company_id == company_id, InterviewSession.status == "active").order_by(InterviewSession.id.desc()))
    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Company not found")
    profile = profile_values(session, company_id)
    classification = classify_company_context(session, company_id)
    if classification.industry and not profile.get("industry"):
        profile["industry"] = classification.industry
    if classification.sub_industry and not profile.get("sub_industry"):
        profile["sub_industry"] = classification.sub_industry
    fingerprint = profile_fingerprint(session, company_id)
    if active and active.context_fingerprint == fingerprint:
        return active
    generation_method = "deterministic-profile"
    generation_note = None
    questions = None
    from signal_stack.config import settings
    use_llm = settings.llm_provider == "ollama" if use_llm is None else use_llm
    if use_llm:
        try:
            from signal_stack.providers.llm.ollama import generate_interview_questions
            questions = _validate_questions(generate_interview_questions(company.name, profile))
            generation_method = "ollama-local"
        except Exception as exc:
            generation_method = "deterministic-fallback"
            generation_note = f"Local interview generation failed validation or timed out ({type(exc).__name__}); deterministic fallback used."
    if questions is None:
        questions = _validate_questions(build_questions(profile, company.name))
    if active:
        for question, (topic, text) in zip(active.questions, questions):
            if question.answered_at is None and question.answer is None:
                question.topic = topic
                question.question = text
                question.follow_up = None
        active.context_fingerprint = fingerprint
        active.generation_method = generation_method
        active.generation_note = generation_note
        session.commit()
        return active
    interview = InterviewSession(company_id=company_id, generation_method=generation_method, generation_note=generation_note)
    interview.context_fingerprint = fingerprint
    session.add(interview)
    session.flush()
    for ordinal, (topic, question) in enumerate(questions, start=1):
        session.add(InterviewQuestion(session_id=interview.id, ordinal=ordinal, topic=topic, question=question))
    session.commit()
    return interview


def current_question(interview: InterviewSession) -> InterviewQuestion | None:
    return next((q for q in interview.questions if q.answered_at is None), None)


def submit_answer(session: Session, question_id: int, answer: str, *, skip: bool = False, follow_up_answer: bool = False) -> str | None:
    question = session.get(InterviewQuestion, question_id)
    if not question:
        raise ValueError("Interview question not found")
    if question.answered_at:
        raise ValueError("This question has already been answered")
    cleaned = answer.strip()
    if not skip and not cleaned:
        raise ValueError("Enter an answer or choose skip")
    if skip:
        question.skipped = True
        question.answer = None
        question.answered_at = utcnow()
        session.commit()
        return None
    if question.follow_up and not follow_up_answer:
        question.answer = cleaned
        session.commit()
        return question.follow_up
    if follow_up_answer:
        question.answer = f"{question.answer or ''}\nFollow-up: {cleaned}".strip()
    else:
        question.answer = cleaned
        vague = cleaned.casefold() in {"not sure", "i don't know", "unknown", "it depends", "depends"}
        if (len(cleaned.split()) < 8 or vague) and question.topic in {"revenue", "segments", "customer_needs", "differentiation", "alternatives", "market_change", "risks", "growth", "internal", "decisions"}:
            question.follow_up = _meaningful_follow_up(question.topic, cleaned)
            session.commit()
            return question.follow_up
    question.answered_at = utcnow()
    if question.topic in {"revenue", "segments", "customer_needs", "differentiation", "alternatives", "market_change", "risks", "growth", "internal", "decisions"}:
        save_profile_value(session, question.session.company_id, f"interview_{question.topic}", question.answer or "", status="founder-provided")
    all_questions = session.scalars(select(InterviewQuestion).where(InterviewQuestion.session_id == question.session_id)).all()
    if all(q.answered_at is not None for q in all_questions):
        question.session.status = "completed"
        question.session.completed_at = utcnow()
        save_profile_version(session, question.session.company_id, "founder interview completed")
    session.commit()
    return None

