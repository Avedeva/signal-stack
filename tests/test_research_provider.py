from signal_stack.models import SourceAudit
from signal_stack.services.company_research import create_company, save_profile_value
from signal_stack.services.public_research import SearchResult, discover_public_research_sources


class FakeSearchProvider:
    name = "fake-search"

    def __init__(self, results):
        self.queries = []
        self.results = results

    def search(self, query: str, *, limit: int = 5):
        self.queries.append(query)
        return self.results


def test_search_results_become_unapproved_sources_with_snippet_provenance(db_session):
    company = create_company(db_session, "Research Provider Co", "https://research-provider.example")
    save_profile_value(db_session, company.id, "industry", "learning technology", status="source-stated", evidence_url=company.website)
    save_profile_value(db_session, company.id, "interview_confidential_goal", "unannounced private launch", status="founder-provided")
    provider = FakeSearchProvider([
        SearchResult(
            title="Learning industry report",
            url="https://publisher.example/report",
            content="A public report about online learning and market changes.",
            published_date="2026-11-10",
            score=0.84,
        ),
        SearchResult(
            title="Duplicate source result",
            url="https://publisher.example/report#top",
            content="A repeated result from another query.",
            published_date=None,
            score=0.72,
        ),
    ])

    result = discover_public_research_sources(db_session, company.id, provider=provider)

    assert len(provider.queries) >= 3
    assert all("unannounced private launch" not in query for query in provider.queries)
    matching = [source for source in result.sources if source.url == "https://publisher.example/report"]
    assert len(matching) == 1
    source = matching[0]
    assert source.status == "candidate"
    assert source.reliability == 0.45
    audit = db_session.query(SourceAudit).filter_by(source_id=source.id, status="search-result-unverified").one()
    assert "learning industry report" in audit.recommendation.casefold()
    assert "2026-11-10" in audit.recommendation
    assert "unverified snippet" in audit.recommendation.casefold()


def test_search_provider_failure_is_reported_without_creating_sources(db_session):
    company = create_company(db_session, "Provider Error Co", "https://provider-error.example")

    class FailingProvider:
        def search(self, query: str, *, limit: int = 5):
            raise RuntimeError("provider unavailable")

    result = discover_public_research_sources(db_session, company.id, provider=FailingProvider())

    assert not result.sources
    assert result.errors
    assert "RuntimeError" in result.errors[0]


def test_search_result_rejects_non_public_or_invalid_schemes():
    import pytest

    from signal_stack.services.public_research import validate_search_result

    with pytest.raises(ValueError, match="Only http and https"):
        validate_search_result({"title": "bad", "url": "javascript:alert(1)", "content": "x"})


def test_tavily_provider_uses_bounded_untrusted_search_results(monkeypatch):
    import json

    import signal_stack.services.public_research as research

    class Response:
        status_code = 200
        closed = False

        def iter_content(self, chunk_size):
            yield json.dumps({"results": [{
                "title": "Public report", "url": "https://publisher.example/story",
                "content": "A reported development.", "published_date": "2026-11-10", "score": 0.8,
            }]}).encode()

        def close(self):
            self.closed = True

    response = Response()
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return response

    monkeypatch.setattr(research.requests, "post", fake_post)
    result = research.TavilySearchProvider("test-secret").search("test query", limit=100)

    assert captured["url"] == research.TAVILY_URL
    assert captured["headers"]["Authorization"] == "Bearer test-secret"
    assert captured["json"]["max_results"] == 10
    assert captured["json"]["include_answer"] is False
    assert captured["json"]["include_raw_content"] is False
    assert captured["timeout"] == (4.0, 12.0)
    assert response.closed
    assert result[0].published_date == "2026-11-10"

