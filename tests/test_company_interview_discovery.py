from sqlalchemy import select

from signal_stack.models import Company, Competitor, InterviewQuestion, InterviewSession, ProfileAttribute, ProfileVersion, Source, SourceAudit, utcnow
from signal_stack.services.company_research import create_company, save_profile_value
from signal_stack.services.competitor_discovery import discover_competitors_from_documents
from signal_stack.services.interview import current_question, start_interview, submit_answer
from signal_stack.services.source_discovery import approve_validated_sources, discover_sources, reassess_sources, set_source_status, validate_source
from signal_stack.models import Document
from signal_stack.services.extraction import content_hash


def test_ten_questions_are_persisted_and_company_specific(db_session):
    first = create_company(db_session, "MeetUniversity", "https://meetuniversity.example")
    second = create_company(db_session, "Zerodha", "https://zerodha.example")
    save_profile_value(db_session, first.id, "industry", "international education", status="founder-provided")
    save_profile_value(db_session, second.id, "industry", "retail investing", status="founder-provided")
    db_session.commit()
    one = start_interview(db_session, first.id)
    two = start_interview(db_session, second.id)
    assert len(one.questions) == len(two.questions) == 10
    assert len({q.question.casefold() for q in one.questions}) == 10
    assert one.questions[0].question != two.questions[0].question
    assert "MeetUniversity" in one.questions[0].question
    assert "Zerodha" in two.questions[0].question


def test_followups_skip_and_answers_persist(db_session):
    company = create_company(db_session, "Company A", "https://a.example")
    interview = start_interview(db_session, company.id)
    question = interview.questions[1]  # customer segment asks for a specific segment
    follow = submit_answer(db_session, question.id, "Students")
    assert follow and question.answered_at is None
    assert "concrete example" in follow
    submit_answer(db_session, question.id, "International applicants are our priority.", follow_up_answer=True)
    assert question.answered_at is not None
    assert "Follow-up" in question.answer
    skipped = interview.questions[2]
    submit_answer(db_session, skipped.id, "", skip=True)
    assert skipped.skipped and skipped.answer is None and skipped.answered_at is not None
    answer = db_session.scalar(select(ProfileAttribute).where(ProfileAttribute.company_id == company.id, ProfileAttribute.key == "interview_segments"))
    assert answer and "International applicants" in answer.value


def test_short_answer_followups_use_the_answer_meaning(db_session):
    first = create_company(db_session, "Interview A", "https://interview-a.example")
    second = create_company(db_session, "Interview B", "https://interview-b.example")
    one = start_interview(db_session, first.id)
    two = start_interview(db_session, second.id)
    follow_one = submit_answer(db_session, one.questions[1].id, "international students")
    follow_two = submit_answer(db_session, two.questions[1].id, "hospital buyers")
    assert follow_one != follow_two
    assert "international students" in follow_one.casefold()
    assert "hospital buyers" in follow_two.casefold()
    assert "concrete example" in follow_one.casefold()


def test_interview_questions_use_evidence_classification_when_industry_label_is_missing(db_session):
    company = create_company(db_session, "Contextual Interview", "https://contextual.example")
    save_profile_value(db_session, company.id, "company_description", "Restaurants use a food delivery marketplace for local dining and grocery orders.", status="source-stated", evidence_url=company.website)
    db_session.commit()
    interview = start_interview(db_session, company.id)
    assert "food services" in interview.questions[5].question.casefold()


def test_context_refresh_updates_only_unanswered_interview_questions(db_session):
    company = create_company(db_session, "Changing Context", "https://changing-context.example")
    save_profile_value(db_session, company.id, "company_description", "Online learning platform for university students and exam preparation.", status="source-stated", evidence_url=company.website)
    db_session.commit()
    interview = start_interview(db_session, company.id)
    original_answered_question = interview.questions[0]
    original_answered_question.answer = "We earn revenue from annual contracts with universities and student support teams."
    original_answered_question.answered_at = utcnow()
    old_market_question = interview.questions[5].question
    db_session.commit()
    save_profile_value(db_session, company.id, "company_description", "Brokerage platform for securities trading, investment accounts, and retail investors.", status="founder-provided", evidence_url=company.website)
    db_session.commit()
    refreshed = start_interview(db_session, company.id)
    assert refreshed.id == interview.id
    assert len(refreshed.questions) == 10
    assert refreshed.questions[0].answer == "We earn revenue from annual contracts with universities and student support teams."
    assert refreshed.questions[0].question == interview.questions[0].question
    assert refreshed.questions[5].question != old_market_question
    assert "financial services" in refreshed.questions[5].question.casefold()


def test_local_interview_timeout_and_invalid_output_use_valid_deterministic_fallback(db_session, monkeypatch):
    import signal_stack.providers.llm.ollama as ollama

    timed_out = create_company(db_session, "Timeout Interview", "https://timeout-interview.example")
    monkeypatch.setattr(ollama, "generate_interview_questions", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("local model timeout")))
    timeout_session = start_interview(db_session, timed_out.id, use_llm=True)
    assert timeout_session.generation_method == "deterministic-fallback"
    assert len(timeout_session.questions) == 10
    assert "TimeoutError" in timeout_session.generation_note

    invalid = create_company(db_session, "Invalid Interview", "https://invalid-interview.example")
    monkeypatch.setattr(ollama, "generate_interview_questions", lambda *args, **kwargs: [("revenue", "Only one question was returned, which is invalid.")])
    invalid_session = start_interview(db_session, invalid.id, use_llm=True)
    assert invalid_session.generation_method == "deterministic-fallback"
    assert len(invalid_session.questions) == 10
    assert "validation" in invalid_session.generation_note.casefold()


def test_discovery_is_context_specific_and_review_state_is_company_scoped(db_session):
    one = create_company(db_session, "Company A", "https://a.example")
    two = create_company(db_session, "Company B", "https://b.example")
    save_profile_value(db_session, one.id, "customer_problem", "visa application delays", status="founder-provided")
    save_profile_value(db_session, one.id, "customer_segments", "Indian graduate students", status="founder-provided")
    save_profile_value(db_session, two.id, "customer_problem", "equity order routing costs", status="founder-provided")
    save_profile_value(db_session, two.id, "customer_segments", "retail investors", status="founder-provided")
    db_session.commit()
    a_sources = discover_sources(db_session, one.id)
    b_sources = discover_sources(db_session, two.id)
    assert {x.category for x in a_sources} == {"customers", "competitors", "market", "internal"}
    assert len(a_sources) == len(b_sources)
    assert a_sources[1].url != b_sources[1].url
    assert "visa application delays" in a_sources[1].reason
    assert all(x.status == "candidate" for x in a_sources)
    foreign = b_sources[0]
    try:
        set_source_status(db_session, one.id, foreign.id, "approved")
        assert False, "cross-company source mutation must fail"
    except ValueError as exc:
        assert "not found" in str(exc)


def test_source_discovery_uses_evidence_classification_and_adds_industry_query(db_session):
    one = create_company(db_session, "Same Name", "https://one.example")
    two = create_company(db_session, "Same Name", "https://two.example")
    save_profile_value(db_session, one.id, "company_description", "Carbon accounting and renewable energy software for industrial emissions reporting.", status="source-stated", evidence_url=one.website)
    save_profile_value(db_session, two.id, "company_description", "Clinics provide telemedicine, diagnostic tools, and medical records for patients.", status="source-stated", evidence_url=two.website)
    db_session.commit()
    one_sources = discover_sources(db_session, one.id)
    two_sources = discover_sources(db_session, two.id)
    one_urls = {source.url for source in one_sources}
    two_urls = {source.url for source in two_sources}
    assert len(one_sources) > 5 and len(two_sources) > 5
    assert one_urls != two_urls
    assert any("carbon" in source.url.casefold() for source in one_sources)
    assert any("healthcare" in source.url.casefold() for source in two_sources)
    assert all(source.status == "candidate" for source in one_sources + two_sources)


def test_context_change_preserves_source_history_and_adds_audit_recommendations(db_session):
    company = create_company(db_session, "Context Co", "https://context.example")
    sources = discover_sources(db_session, company.id)
    sources[0].last_status = "healthy"
    sources[0].last_checked_at = utcnow()
    db_session.commit()
    set_source_status(db_session, company.id, sources[0].id, "approved")
    save_profile_value(db_session, company.id, "customer_problem", "initial focus", status="founder-provided")
    db_session.commit()
    audits = reassess_sources(db_session, company.id, "target segment changed")
    versions = db_session.scalars(select(ProfileVersion).where(ProfileVersion.company_id == company.id)).all()
    assert versions[-1].reason == "target segment changed"
    assert audits
    assert db_session.get(Source, sources[0].id).status == "approved"
    assert db_session.scalar(select(SourceAudit).where(SourceAudit.company_id == company.id)) is not None


def test_public_source_requires_recent_successful_validation_before_approval(db_session):
    company = create_company(db_session, "Validation Co", "https://validation.example")
    source = discover_sources(db_session, company.id)[0]
    try:
        set_source_status(db_session, company.id, source.id, "approved")
        assert False, "an unchecked source must not be approved"
    except ValueError as exc:
        assert "Validate this public source" in str(exc)
    source.last_status = "healthy"
    source.last_checked_at = utcnow()
    db_session.commit()
    approved = set_source_status(db_session, company.id, source.id, "approved")
    assert approved.status == "approved"


def test_source_validation_records_health_and_does_not_auto_approve(db_session, monkeypatch):
    from signal_stack.services.security import FetchResult
    import signal_stack.services.security as security
    company = create_company(db_session, "Validate Co", "https://validate.example")
    source = discover_sources(db_session, company.id)[0]
    monkeypatch.setattr(security, "fetch_public_url", lambda url, max_bytes=None: FetchResult(url, 200, {"Content-Type": "text/html"}, b"<html>page</html>"))
    validated, audit = validate_source(db_session, company.id, source.id)
    assert validated.last_status == "healthy"
    assert validated.status == "candidate"
    assert audit.status == "candidate-healthy"


def test_batch_approval_only_approves_recently_validated_company_sources(db_session):
    one = create_company(db_session, "Batch A", "https://batch-a.example")
    two = create_company(db_session, "Batch B", "https://batch-b.example")
    first = discover_sources(db_session, one.id)[0]
    second = discover_sources(db_session, one.id)[1]
    unchecked = discover_sources(db_session, two.id)[0]
    for source in (first, second):
        source.last_status = "healthy"
        source.last_checked_at = utcnow()
    db_session.commit()
    approved = approve_validated_sources(db_session, one.id, [first.id, second.id])
    assert {source.id for source in approved} == {first.id, second.id}
    assert first.status == second.status == "approved"
    assert unchecked.status == "candidate"
    try:
        approve_validated_sources(db_session, one.id, [unchecked.id])
        assert False, "cross-company sources must not be approved"
    except ValueError as exc:
        assert "not found" in str(exc)


def test_company_research_extracts_source_stated_profile_and_first_party_feeds(db_session, monkeypatch):
    from signal_stack.services.company_research import research_company
    from signal_stack.services.security import FetchResult
    import signal_stack.services.company_research as research
    company = create_company(db_session, "Research Co", "https://research.example")
    discover_sources(db_session, company.id)
    html = b'<html><head><title>Research Co</title><meta name="description" content="Guidance for global applicants"/><link rel="alternate" type="application/rss+xml" href="/updates/feed.xml"/></head><body><h1>Study planning</h1><p>Applications and scholarships.</p><a href="/blog">Blog</a></body></html>'
    monkeypatch.setattr(research, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "text/html"}, html))
    observations = research_company(db_session, company)
    description = db_session.scalar(select(ProfileAttribute).where(ProfileAttribute.company_id == company.id, ProfileAttribute.key == "company_description"))
    assert observations["website_title"] == "Research Co"
    assert description.status == "source-stated"
    assert description.evidence_url == company.website
    assert "Guidance" in description.value
    assert db_session.query(Document).filter_by(company_id=company.id).count() == 1
    assert db_session.scalar(select(Source).where(Source.company_id == company.id, Source.url == "https://research.example/updates/feed.xml")) is not None


def test_browser_research_fixture_is_profile_only_and_creates_adaptive_questions(db_session, monkeypatch):
    from pathlib import Path
    import signal_stack.cli as cli
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False))
    fixture = Path(__file__).resolve().parents[1] / "demo_evidence" / "company_profiles.json"
    cli.import_profile_evidence(str(fixture))
    companies = db_session.scalars(select(Company).order_by(Company.name)).all()
    assert len(companies) == 3
    meet = next(c for c in companies if c.name == "MeetUniversity")
    profile = db_session.scalar(select(ProfileAttribute).where(ProfileAttribute.company_id == meet.id, ProfileAttribute.key == "customer_problem"))
    assert profile.status == "browser-stated"
    assert profile.evidence_url == "https://www.meetuniversity.com/about"
    assert "international education" in profile.value
    interview = db_session.scalar(select(InterviewSession).where(InterviewSession.company_id == meet.id))
    assert len(interview.questions) == 10
    assert db_session.query(Document).filter_by(company_id=meet.id).count() == 0


def test_context_change_flags_declining_source_relevance_without_removing_source(db_session):
    company = create_company(db_session, "Relevance Co", "https://relevance.example")
    save_profile_value(db_session, company.id, "customer_problem", "admission application delays", status="founder-provided")
    save_profile_value(db_session, company.id, "customer_segments", "international students", status="founder-provided")
    db_session.commit()
    sources = discover_sources(db_session, company.id)
    customer_source = next(s for s in sources if s.category == "customers")
    assert customer_source.relevance > .4
    customer_source.last_status = "healthy"
    customer_source.last_checked_at = utcnow()
    db_session.commit()
    set_source_status(db_session, company.id, customer_source.id, "approved")
    old_relevance = customer_source.relevance
    save_profile_value(db_session, company.id, "customer_problem", "blockchain settlement fees", status="founder-provided")
    save_profile_value(db_session, company.id, "customer_segments", "crypto traders", status="founder-provided")
    db_session.commit()
    reassess_sources(db_session, company.id, "strategy shifted to crypto traders")
    assert customer_source.relevance < old_relevance
    assert customer_source.status == "approved"
    audit = db_session.scalar(select(SourceAudit).where(SourceAudit.source_id == customer_source.id, SourceAudit.status == "relevance-declined"))
    assert audit is not None


def test_explicit_competitor_mentions_become_cited_candidates(db_session):
    company = create_company(db_session, "Brand A", "https://brand-a.example")
    source = next(x for x in discover_sources(db_session, company.id) if x.category == "competitors")
    doc = Document(company_id=company.id, source_id=source.id, canonical_url="https://news.example/compare", title="Brand A vs Brand B", content="Brand A vs Brand B are compared for the same customer use case.", content_hash=content_hash("Brand A vs Brand B are compared for the same customer use case."), category="competitors", source_reliability=.5)
    db_session.add(doc)
    db_session.commit()
    found = discover_competitors_from_documents(db_session, company.id)
    assert len(found) == 1
    assert found[0].name == "Brand B"
    assert found[0].evidence_url == doc.canonical_url
    assert found[0].status == "candidate"
    assert found[0].supporting_source_urls_json == '["https://news.example/compare"]'
    assert found[0].products_services == ""
    assert found[0].target_market == ""
    assert found[0].geographic_focus == ""
    assert found[0].discovered_at is not None


def test_competitor_discovery_rejects_partner_customer_supplier_and_investor_mentions(db_session):
    company = create_company(db_session, "Brand A", "https://brand-a.example")
    source = next(x for x in discover_sources(db_session, company.id) if x.category == "competitors")
    examples = [
        ("https://news.example/partner", "Brand A vs PartnerCo", "PartnerCo is a supplier to Brand A, not a competitor."),
        ("https://news.example/customer", "Brand A compared with ClientCo", "ClientCo is a customer of Brand A and is not an alternative."),
        ("https://news.example/investor", "Alternatives to Brand A", "InvestorCo funds Brand A and does not compete in this market."),
    ]
    for url, title, content in examples:
        db_session.add(Document(company_id=company.id, source_id=source.id, canonical_url=url, title=title, content=content, content_hash=content_hash(content), category="competitors", source_reliability=.5))
    db_session.commit()

    assert discover_competitors_from_documents(db_session, company.id) == []


def test_competitor_candidates_keep_type_evidence_confidence_and_manual_edits(db_session):
    from signal_stack.services.competitor_discovery import update_competitor

    company = create_company(db_session, "Company X", "https://company-x.example")
    source = next(x for x in discover_sources(db_session, company.id) if x.category == "competitors")
    records = [
        ("https://news.example/direct", "Company X vs DirectCo", "DirectCo is a direct competitor serving the same customer segment with a similar product."),
        ("https://news.example/substitute", "Alternatives to Company X", "Alternatives to Company X include SubstituteCo, which customers use instead of the same service."),
        ("https://news.example/emerging", "Company X vs EmergingCo", "EmergingCo is a new entrant launching into the category and may compete for the same buyers."),
    ]
    for url, title, content in records:
        db_session.add(Document(company_id=company.id, source_id=source.id, canonical_url=url, title=title, content=content, content_hash=content_hash(content), category="competitors", source_reliability=.5))
    db_session.commit()
    found = discover_competitors_from_documents(db_session, company.id)
    by_name = {candidate.name: candidate for candidate in found}
    assert by_name["DirectCo"].competitor_type == "direct"
    assert by_name["SubstituteCo"].competitor_type == "substitute"
    assert by_name["EmergingCo"].competitor_type == "emerging"
    assert all(candidate.evidence_snippet and candidate.evidence_url and 0 < candidate.discovery_confidence <= 1 for candidate in found)
    edited = update_competitor(
        db_session, company.id, by_name["EmergingCo"].id, name="Emerging Co Inc", website="https://emerging.example",
        competitor_type="indirect", rationale="Founder-reviewed as an indirect substitute.",
        profile_fields={
            "products_services": "Education support software",
            "target_market": "International applicants",
            "customer_segments": "University applicants",
            "geographic_focus": "India",
            "competitive_relationship": "Same buyer segment; source describes a comparison.",
        },
        supporting_source_urls=["https://news.example/emerging"],
    )
    assert edited.name == "Emerging Co Inc" and edited.website == "https://emerging.example/"
    assert edited.competitor_type == "indirect" and edited.status == "candidate"
    assert edited.evidence_url == "https://news.example/emerging"
    assert edited.products_services == "Education support software"
    assert edited.target_market == "International applicants"
    assert edited.customer_segments == "University applicants"
    assert edited.geographic_focus == "India"
    assert edited.supporting_source_urls_json == '["https://news.example/emerging"]'


def test_competitor_profiles_reject_duplicate_website_domains(db_session):
    import pytest
    from signal_stack.models import Competitor
    from signal_stack.services.competitor_discovery import update_competitor

    company = create_company(db_session, "Duplicate Profile Co", "https://duplicate-profile.example")
    first = Competitor(company_id=company.id, name="First", website="https://first.example/", rationale="Evidence A", status="candidate")
    second = Competitor(company_id=company.id, name="Second", rationale="Evidence B", status="candidate")
    db_session.add_all([first, second])
    db_session.commit()

    with pytest.raises(ValueError, match="same website domain"):
        update_competitor(
            db_session, company.id, second.id, name="Second", website="https://first.example/about",
            competitor_type="unclassified", rationale="Evidence B",
        )

