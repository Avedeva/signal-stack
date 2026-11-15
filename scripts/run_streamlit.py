"""Start Streamlit after loading Signal Stack environment configuration."""
from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_stack.config import settings  # noqa: E402


if settings.environment == "production":
    os.environ["STREAMLIT_SERVER_COOKIESECRET"] = settings.session_secret

from streamlit.web import cli as streamlit_cli  # noqa: E402


sys.argv = [
    "streamlit",
    "run",
    str(ROOT / "app.py"),
    "--server.address",
    settings.bind_address,
]
streamlit_cli.main()
