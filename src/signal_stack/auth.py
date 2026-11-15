"""Authentication helpers for the Streamlit single-founder deployment model."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Mapping


PBKDF2_ITERATIONS = 310_000
_HASH_SCHEME = "pbkdf2_sha256"


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    if not password or len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    if iterations < 100_000:
        raise ValueError("Password hashing work factor is too low")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_HASH_SCHEME}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        iterations = int(raw_iterations)
        if scheme != _HASH_SCHEME or iterations < 100_000 or iterations > 5_000_000:
            return False
        salt = bytes.fromhex(raw_salt)
        expected = bytes.fromhex(raw_digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError):
        return False


def validate_deployment_security(
    *, environment: str, password_hash: str, session_secret: str, bind_address: str = "127.0.0.1"
) -> None:
    mode = environment.strip().casefold()
    if mode not in {"development", "production"}:
        raise ValueError("SIGNAL_STACK_ENVIRONMENT must be development or production")
    if mode == "development":
        if bind_address.strip().casefold() not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Development mode must bind to a loopback address")
        return
    if not password_hash.startswith(f"{_HASH_SCHEME}$"):
        raise ValueError("Production requires a configured password hash")
    if len(session_secret) < 32:
        raise ValueError("Production requires a session secret of at least 32 characters")


def session_is_valid(state: Mapping[str, object], *, now: float | None = None, max_idle_seconds: int = 1800) -> bool:
    if state.get("authenticated") is not True:
        return False
    last_seen = state.get("last_seen")
    if not isinstance(last_seen, (int, float)):
        return False
    elapsed = (time.monotonic() if now is None else now) - float(last_seen)
    return 0 <= elapsed < max_idle_seconds


def authenticate_streamlit(st) -> bool:
    """Gate application rendering and stamp the active session with an ORM principal."""
    from signal_stack.config import settings

    try:
        validate_deployment_security(
            environment=settings.environment,
            password_hash=settings.auth_password_hash,
            session_secret=settings.session_secret,
            bind_address=settings.bind_address,
        )
    except ValueError as exc:
        st.error("Security configuration prevents startup:")
        st.text(str(exc))
        return False

    state = st.session_state
    if session_is_valid(state, max_idle_seconds=settings.session_timeout_minutes * 60):
        state["last_seen"] = time.monotonic()
        return True
    if state.get("authenticated"):
        state.clear()
        st.warning("Your session expired. Sign in again.")

    if not settings.auth_password_hash and settings.environment == "development":
        state["authenticated"] = True
        state["principal"] = "local-development"
        state["last_seen"] = time.monotonic()
        return True

    st.subheader("Sign in to Signal Stack")
    with st.form("signal_stack_login"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if verify_password(password, settings.auth_password_hash):
            state["authenticated"] = True
            state["principal"] = "founder-admin"
            state["last_seen"] = time.monotonic()
            st.rerun()
        else:
            st.error("Invalid credentials")
    return False


def logout_streamlit(st) -> None:
    st.session_state.clear()
    st.rerun()
