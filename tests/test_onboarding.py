from sqlalchemy import select

from signal_stack.models import Company, InterviewQuestion, InterviewSession, OnboardingStage, Source, utcnow
from signal_stack.services.company_research import create_company, save_profile_value
from signal_stack.services.onboarding import run_onboarding
from signal_stack.services.briefs import generate_brief
from signal_stack.services.intelligence import crawl_source
from signal_stack.services.security import FetchResult
from signal_stack.services.source_discovery import set_source_status, validate_source


def test_onboarding_records_failure_continues_and_retries_only_failed_stage(db_session, monkeypatch):
    company = create_company(db_session, "Fourth Co", "https://fourth.example")
    calls = {"research": 0, "source_discovery": 0}
    from signal_stack.services import onboarding as onboarding_module
    original_discovery = onboarding_module.discover_sources

    def counted_discovery(session, company_id):
        calls["source_discovery"] += 1
        return original_discovery(session, company_id)

    def flaky_research(session, selected):
        calls["research"] += 1
        if calls["research"] == 1:
            raise RuntimeError("temporary network failure")
        save_profile_value(session, selected.id, "industry", "space logistics", status="source-stated", evidence_url=selected.website, evidence_snippet="space logistics")
        return {"website_title": "Fourth Co"}

    monkeypatch.setattr("signal_stack.services.onboarding.research_company", flaky_research)
    monkeypatch.setattr("signal_stack.services.onboarding.discover_sources", counted_discovery)
    first = run_onboarding(db_session, company.id)
    assert first["research"].status == "failed"
    assert first["source_discovery"].status == "awaiting-evidence"
    assert first["classification"].status == "succeeded"
    assert first["interview"].status == "succeeded"
    assert len(db_session.scalars(select(InterviewSession).where(InterviewSession.company_id == company.id)).all()) == 1
    first_answer = db_session.scalar(select(InterviewQuestion).join(InterviewSession).where(InterviewSession.company_id == company.id).order_by(InterviewQuestion.ordinal))
    first_answer.answer = "Our operating team coordinates launches with several regional logistics partners."
    first_answer.answered_at = utcnow()
    db_session.commit()
    old_source_ids = set(db_session.scalars(select(Source.id).where(Source.company_id == company.id)).all())
    retained_source = db_session.get(Source, next(iter(old_source_ids)))
    retained_source.status = "approved"
    db_session.commit()

    second = run_onboarding(db_session, company.id)
    assert second["research"].status == "succeeded"
    assert second["source_discovery"].status == "succeeded"
    assert second["interview"].status == "succeeded"
    assert calls["source_discovery"] == 2
    all_sources = db_session.scalars(select(Source).where(Source.company_id == company.id)).all()
    assert old_source_ids <= {source.id for source in all_sources}
    assert len({source.url for source in all_sources}) == len(all_sources)
    assert db_session.get(Source, retained_source.id).status == "approved"
    assert len(db_session.scalars(select(InterviewSession).where(InterviewSession.company_id == company.id)).all()) == 1
    assert first_answer.answer == "Our operating team coordinates launches with several regional logistics partners."
    stage = db_session.scalar(select(OnboardingStage).where(OnboardingStage.company_id == company.id, OnboardingStage.stage == "research"))
    assert stage.attempts == 2
    assert stage.error is None


def test_onboarding_is_company_scoped(db_session, monkeypatch):
    one = create_company(db_session, "One Co", "https://one.example")
    two = create_company(db_session, "Two Co", "https://two.example")
    monkeypatch.setattr("signal_stack.services.onboarding.research_company", lambda session, company: {})
    run_onboarding(db_session, one.id)
    assert db_session.query(OnboardingStage).filter_by(company_id=one.id).count() >= 3
    assert db_session.query(OnboardingStage).filter_by(company_id=two.id).count() == 0


def test_unseen_company_runs_local_fixture_to_evidence_brief_without_cross_company_leakage(db_session, monkeypatch):
    from signal_stack.models import Competitor, Signal
    from signal_stack.services.company_research import create_company
    import signal_stack.services.company_research as research
    import signal_stack.services.security as security
    import signal_stack.services.intelligence as intelligence

    company = create_company(db_session, "Northstar", "https://northstar.example")
    other = create_company(db_session, "Elsewhere Co", "https://elsewhere.example")
    html = b'<html><head><title>Northstar</title><meta name="description" content="Northstar sells cold storage systems for public agencies." /></head><body><h1>Cold storage systems</h1></body></html>'
    monkeypatch.setattr(research, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "text/html"}, html))
    run_onboarding(db_session, company.id)
    assert db_session.query(InterviewSession).filter_by(company_id=company.id).count() == 1
    assert db_session.query(Source).filter_by(company_id=company.id).count() >= 4
    assert db_session.query(Source).filter_by(company_id=other.id).count() == 0

    candidate = db_session.scalar(select(Source).where(Source.company_id == company.id, Source.category == "competitors"))
    monkeypatch.setattr(security, "fetch_public_url", lambda url, max_bytes=None: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, b"<rss/>"))
    validated, _audit = validate_source(db_session, company.id, candidate.id)
    assert validated.status == "candidate" and validated.last_status == "healthy"
    set_source_status(db_session, company.id, candidate.id, "approved")
    rss = b'<rss version="2.0"><channel><item><title>Northstar vs PackWorks</title><link>https://publisher.example/northstar-packworks</link><description>Northstar and PackWorks announced a new partnership in cold storage systems.</description><pubDate>Fri, 13 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>'
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, rss))
    run = crawl_source(db_session, company.id, candidate.id)
    assert run.status == "succeeded" and run.documents_found == 1
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 1
    discovered = db_session.scalar(select(Competitor).where(Competitor.company_id == company.id, Competitor.name == "PackWorks"))
    assert discovered and discovered.evidence_url == "https://publisher.example/northstar-packworks"
    assert db_session.query(Competitor).filter_by(company_id=other.id).count() == 0
    brief = generate_brief(db_session, company.id)
    assert brief.signal_count == 1
    assert "Northstar vs PackWorks" in brief.markdown
    assert "https://publisher.example/northstar-packworks" in brief.markdown
