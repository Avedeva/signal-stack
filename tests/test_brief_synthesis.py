import json
from types import SimpleNamespace

import pytest
import signal_stack.config as config
import signal_stack.providers.llm.brief_synthesis as provider
import signal_stack.services.briefs as briefs
from signal_stack.models import Development, Document, ProfileAttribute, Signal, Source
from signal_stack.services.company_research import create_company
from signal_stack.services.briefs import generate_brief


def _response(payload):
    body = json.dumps(payload).encode("utf-8")

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            for offset in range(0, len(body), chunk_size):
                yield body[offset:offset + chunk_size]

        def close(self):
            pass

    return Response()


def test_openrouter_synthesis_requires_a_matching_evidence_quote(monkeypatch):
    monkeypatch.setattr(provider, "settings", SimpleNamespace(llm_provider="openrouter", openrouter_api_key="test-key", openrouter_model="perplexity/pplx-decider-flash-v1.3:free", openrouter_brief_model="openai/gpt-oss-120b:free"))
    seen = {}

    def post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _response({"choices": [{"message": {"content": json.dumps({"signals": [
            {
                "signal_id": 21,
                "headline": "Publisher reports expanded service coverage",
                "why_it_matters": "Potentially changes options for the monitored customer segment.",
                "next_step": "Review the original announcement and compare it with current customer evidence.",
                "watch_for": "A follow-up showing regional availability.",
                "evidence_quote": "The provider expanded service coverage for regional customers.",
            },
            {
                "signal_id": 22,
                "headline": "Unsupported claim",
                "why_it_matters": "This output has no matching source text.",
                "next_step": "Do not act.",
                "watch_for": "Nothing.",
                "evidence_quote": "Revenue increased by 40%.",
            },
        ]})}}]})

    monkeypatch.setattr(provider.requests, "post", post)
    outputs = provider.synthesize_public_signals(
        [{"signal_id": 21, "title": "Service change", "category": "market", "source_type": "rss", "signal_type": "neutral", "source_title": "Publisher", "excerpt": "The provider expanded service coverage for regional customers."},
         {"signal_id": 22, "title": "Unsupported claim", "category": "market", "source_type": "rss", "signal_type": "neutral", "source_title": "Publisher", "excerpt": "The provider opened a new office."}],
        company_context={"company_name": "Example Co", "public_profile": {"industry": "education"}},
    )

    assert set(outputs) == {21}
    assert outputs[21].headline == "Publisher reports expanded service coverage"
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    request = seen["json"]
    assert request["model"] == "openai/gpt-oss-120b:free"
    assert request["response_format"] == {"type": "json_object"}
    system_prompt = request["messages"][0]["content"].casefold()
    assert "untrusted" in system_prompt and "never follow" in system_prompt
    assert "The provider expanded service coverage" in request["messages"][1]["content"]


def test_synthesis_refuses_a_model_without_free_variant(monkeypatch):
    monkeypatch.setattr(provider, "settings", SimpleNamespace(llm_provider="openrouter", openrouter_api_key="test-key", openrouter_brief_model="vendor/paid-model"))
    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("paid model request must not be sent")))

    with pytest.raises(provider.BriefSynthesisError, match=":free suffix"):
        provider.synthesize_public_signals(
            [{"signal_id": 5, "source_type": "rss", "category": "market", "excerpt": "A publisher reports that service coverage expanded for customers."}],
            company_context={},
        )


def _add_signal(session, company, *, category, source_type, title, content, url):
    source = Source(
        company_id=company.id, url=url, title=f"{title} publisher", category=category,
        source_type=source_type, reason="test source", reliability=.8, relevance=.8, status="approved",
    )
    session.add(source)
    session.flush()
    document = Document(
        company_id=company.id, source_id=source.id, canonical_url=url, title=title,
        content_hash=("a" if category == "market" else "b") * 64, content=content,
        category=category, source_reliability=.8,
    )
    session.add(document)
    session.flush()
    development = Development(
        company_id=company.id, document_id=document.id, what_happened=title,
        business_implication="Potentially relevant; review the source.", signal_type="neutral",
        factual_claim=f"Source text reports: {title}.", interpretation="Potential implication needs review.",
        affected_business_area="Leadership review", recommended_action="Review the source.",
        analysis_confidence=.35, confidence_rationale="One source; unverified.",
        analysis_method="test", signal_classification_method="deterministic-rules",
    )
    session.add(development)
    session.flush()
    signal = Signal(
        company_id=company.id, development_id=development.id, category=category,
        relevance=.5, impact=.5, urgency=.5, source_reliability=.8, evidence_strength=.7,
        novelty=.5, recency=.5, score=.55, score_reason="Test score from visible dimensions.",
    )
    session.add(signal)
    session.flush()
    return signal


def test_brief_llm_path_sends_only_public_signals_and_public_profile(db_session, monkeypatch):
    company = create_company(db_session, "Example Company", "https://example.test")
    public_signal = _add_signal(
        db_session, company, category="market", source_type="rss", title="Service expansion",
        content="The provider expanded service coverage for regional customers.", url="https://publisher.test/story",
    )
    _add_signal(
        db_session, company, category="internal", source_type="internal", title="Private sales note",
        content="Founder confidential sales note: lost deal budget was $900,000.", url="internal://upload/private",
    )
    db_session.add_all([
        ProfileAttribute(company_id=company.id, key="industry", value="education technology", status="source-stated"),
        ProfileAttribute(company_id=company.id, key="growth_priority", value="confidential expansion target", status="founder-provided"),
    ])
    db_session.commit()
    captured = {}

    def synthesize(records, *, company_context):
        captured["records"] = records
        captured["context"] = company_context
        return {
            public_signal.id: provider.SignalSynthesis(
                headline="Publisher reports expanded regional coverage",
                why_it_matters="This may change available customer options.",
                next_step="Check whether current customer priorities overlap with the regions named.",
                watch_for="A follow-up with launch dates.",
                evidence_quote="The provider expanded service coverage for regional customers.",
            )
        }

    monkeypatch.setattr(briefs, "synthesize_public_signals", synthesize)
    monkeypatch.setattr(config, "settings", SimpleNamespace(llm_provider="openrouter", openrouter_api_key="test-key", openrouter_model="test-model"))
    brief = generate_brief(db_session, company.id, use_llm=True)

    assert [item["signal_id"] for item in captured["records"]] == [public_signal.id]
    assert "Founder confidential" not in repr(captured)
    assert "confidential expansion target" not in repr(captured)
    assert captured["context"]["public_profile"]["industry"] == "education technology"
    assert "AI-assisted interpretation" in brief.markdown
    assert "expanded regional coverage" in brief.markdown
    assert "The provider expanded service coverage for regional customers." in brief.markdown
    assert "Founder confidential sales note" in brief.markdown


def test_brief_llm_failure_keeps_deterministic_report_and_marks_fallback(db_session, monkeypatch):
    company = create_company(db_session, "Fallback Company", "https://fallback.test")
    _add_signal(
        db_session, company, category="market", source_type="rss", title="Service expansion",
        content="The provider expanded service coverage for regional customers.", url="https://publisher.test/fallback",
    )
    monkeypatch.setattr(briefs, "synthesize_public_signals", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")))
    monkeypatch.setattr(config, "settings", SimpleNamespace(llm_provider="openrouter", openrouter_api_key="test-key", openrouter_model="test-model"))

    brief = generate_brief(db_session, company.id, use_llm=True)

    assert "OpenRouter synthesis unavailable" in brief.markdown
    assert "Service expansion" in brief.markdown
    assert brief.signal_count == 1


def test_brief_llm_request_with_no_public_signals_does_not_call_provider(db_session, monkeypatch):
    company = create_company(db_session, "No Signal Company", "https://nosignal.test")
    monkeypatch.setattr(briefs, "synthesize_public_signals", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call provider")))
    monkeypatch.setattr(config, "settings", SimpleNamespace(llm_provider="openrouter", openrouter_api_key="test-key", openrouter_model="test-model"))

    brief = generate_brief(db_session, company.id, use_llm=True)

    assert "No public signals were eligible; no model request was sent" in brief.markdown
    assert "coverage gap" in brief.markdown.casefold()
