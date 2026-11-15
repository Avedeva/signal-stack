from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_env_file() -> None:
    """Load simple KEY=value settings without replacing variables already set by the shell."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, raw = value.split("=", 1)
        key = key.strip()
        raw = raw.strip().strip("\"'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, raw)


_load_env_file()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("SIGNAL_STACK_DATABASE_URL", "sqlite:///data/signal_stack.db")
    data_dir: Path = Path(os.getenv("SIGNAL_STACK_DATA_DIR", str(ROOT / "data")))
    reports_dir: Path = Path(os.getenv("SIGNAL_STACK_REPORTS_DIR", str(ROOT / "reports")))
    user_agent: str = os.getenv(
        "SIGNAL_STACK_USER_AGENT", "SignalStackResearchBot/0.1 (respectful public-source monitoring)"
    )
    request_timeout: float = float(os.getenv("SIGNAL_STACK_REQUEST_TIMEOUT", "12"))
    max_response_bytes: int = int(os.getenv("SIGNAL_STACK_MAX_RESPONSE_BYTES", "2000000"))
    domain_delay_seconds: float = float(os.getenv("SIGNAL_STACK_DOMAIN_DELAY_SECONDS", "1.5"))
    total_request_deadline_seconds: float = float(os.getenv("SIGNAL_STACK_TOTAL_REQUEST_DEADLINE_SECONDS", "30"))
    robots_cache_capacity: int = int(os.getenv("SIGNAL_STACK_ROBOTS_CACHE_CAPACITY", "1024"))
    host_pacing_capacity: int = int(os.getenv("SIGNAL_STACK_HOST_PACING_CAPACITY", "2048"))
    default_timezone: str = os.getenv("SIGNAL_STACK_DEFAULT_TIMEZONE", "Asia/Kolkata")
    environment: str = os.getenv("SIGNAL_STACK_ENVIRONMENT", "development").lower()
    bind_address: str = os.getenv("SIGNAL_STACK_BIND_ADDRESS", "127.0.0.1")
    auth_password_hash: str = os.getenv("SIGNAL_STACK_AUTH_PASSWORD_HASH", "")
    session_secret: str = os.getenv("SIGNAL_STACK_SESSION_SECRET", "")
    session_timeout_minutes: int = int(os.getenv("SIGNAL_STACK_SESSION_TIMEOUT_MINUTES", "30"))
    llm_provider: str = os.getenv("SIGNAL_STACK_LLM_PROVIDER", "openrouter").lower()
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model: str = os.getenv("SIGNAL_STACK_OPENROUTER_MODEL", "perplexity/pplx-decider-flash-v1.3:free")
    openrouter_brief_model: str = os.getenv("SIGNAL_STACK_OPENROUTER_BRIEF_MODEL", "openai/gpt-oss-120b:free")
    research_search_provider: str = os.getenv("SIGNAL_STACK_RESEARCH_SEARCH_PROVIDER", "none").lower()
    tavily_api_key: str = os.getenv("SIGNAL_STACK_TAVILY_API_KEY", "")
    ollama_url: str = os.getenv("SIGNAL_STACK_OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("SIGNAL_STACK_OLLAMA_MODEL", "")

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()

