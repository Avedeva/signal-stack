from types import SimpleNamespace

import pytest
import signal_stack.config as config
import signal_stack.providers.llm.openrouter as provider
from signal_stack.services.intelligence import classify_with_model, classify_with_model_details


def test_openrouter_decisions_request_returns_only_allowed_classification(monkeypatch):
    monkeypatch.setattr(provider, "settings", SimpleNamespace(openrouter_api_key="test-key", openrouter_model="perplexity/pplx-decider-flash-v1.3:free"))
    seen = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"answers": {"signal_type": {"choice": "threat"}}}

    def post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr(provider.requests, "post", post)
    assert provider.classify_signal("Service outage", "The service was suspended.", "market") == "threat"
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["json"]["model"] == "perplexity/pplx-decider-flash-v1.3:free"
    assert seen["json"]["state"]["title"] == "Service outage"
    instructions = seen["json"]["questions"]["signal_type"]["instructions"].casefold()
    assert "untrusted" in instructions
    assert "never follow" in instructions
    assert "evidence" in instructions


def test_signal_classifier_refuses_non_free_model_before_request(monkeypatch):
    monkeypatch.setattr(provider, "settings", SimpleNamespace(openrouter_api_key="test-key", openrouter_model="vendor/paid-model"))
    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("paid model request must not be sent")))

    with pytest.raises(ValueError, match="free suffix"):
        provider.classify_signal("A headline", "Public evidence", "market")


def test_model_failure_falls_back_to_local_classifier(monkeypatch):
    monkeypatch.setattr(config, "settings", SimpleNamespace(llm_provider="openrouter", openrouter_api_key="test-key", openrouter_model="perplexity/pplx-decider-flash-v1.3:free"))
    monkeypatch.setattr(provider, "classify_signal", lambda *args: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert classify_with_model("Lawsuit filed", "A court filed a lawsuit.", "market") == "threat"


def test_internal_content_never_calls_external_model(monkeypatch):
    monkeypatch.setattr(config, "settings", SimpleNamespace(llm_provider="openrouter", openrouter_api_key="test-key", openrouter_model="perplexity/pplx-decider-flash-v1.3:free"))
    monkeypatch.setattr(provider, "classify_signal", lambda *args: (_ for _ in ()).throw(AssertionError("external provider called")))
    assert classify_with_model("Internal note", "A routine comment", "internal") == "neutral"


def test_invalid_model_choice_falls_back_and_records_classifier_method(monkeypatch):
    monkeypatch.setattr(config, "settings", SimpleNamespace(llm_provider="openrouter", openrouter_api_key="test-key", openrouter_model="perplexity/pplx-decider-flash-v1.3:free"))
    monkeypatch.setattr(provider, "settings", SimpleNamespace(openrouter_api_key="test-key", openrouter_model="perplexity/pplx-decider-flash-v1.3:free"))

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"answers": {"signal_type": {"choice": "unsupported-label"}}}

    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: Response())
    value, method = classify_with_model_details("Lawsuit filed", "A lawsuit may disrupt company operations.", "market")
    assert value == "threat"
    assert method == "deterministic-fallback"


def test_valid_openrouter_decision_records_provider_method(monkeypatch):
    monkeypatch.setattr(config, "settings", SimpleNamespace(llm_provider="openrouter", openrouter_api_key="test-key", openrouter_model="perplexity/pplx-decider-flash-v1.3:free"))
    monkeypatch.setattr(provider, "settings", SimpleNamespace(openrouter_api_key="test-key", openrouter_model="perplexity/pplx-decider-flash-v1.3:free"))

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"answers": {"signal_type": {"choice": "opportunity"}}}

    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: Response())
    value, method = classify_with_model_details("New partnership", "A company announced a partnership.", "market")
    assert value == "opportunity"
    assert method == "openrouter-decisions"
