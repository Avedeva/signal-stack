from signal_stack.config import Settings


def test_ollama_configuration_has_loopback_default_and_optional_model():
    settings = Settings()
    assert settings.ollama_url == "http://127.0.0.1:11434"
    assert isinstance(settings.ollama_model, str)


def test_public_research_provider_is_disabled_by_default():
    settings = Settings()
    assert settings.research_search_provider == "none"
    assert settings.tavily_api_key == ""
