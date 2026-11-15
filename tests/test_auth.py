from dataclasses import replace

import pytest
from sqlalchemy import select

import signal_stack.database as database
from signal_stack.auth import hash_password, session_is_valid, validate_deployment_security, verify_password
from signal_stack.config import settings
from signal_stack.models import Company


def test_password_hashing_uses_salted_pbkdf2_and_verifies_without_plaintext_storage():
    encoded = hash_password("long-and-unique-passphrase")

    assert encoded.startswith("pbkdf2_sha256$")
    assert "long-and-unique-passphrase" not in encoded
    assert verify_password("long-and-unique-passphrase", encoded)
    assert not verify_password("wrong", encoded)
    assert hash_password("long-and-unique-passphrase") != encoded


def test_network_deployment_fails_closed_without_credentials_or_secret():
    with pytest.raises(ValueError, match="password hash"):
        validate_deployment_security(environment="production", password_hash="", session_secret="configured")
    with pytest.raises(ValueError, match="session secret"):
        validate_deployment_security(environment="production", password_hash="pbkdf2_sha256$1$x$y", session_secret="")


def test_local_development_mode_requires_loopback_binding():
    validate_deployment_security(environment="development", password_hash="", session_secret="", bind_address="127.0.0.1")
    with pytest.raises(ValueError, match="loopback"):
        validate_deployment_security(environment="development", password_hash="", session_secret="", bind_address="0.0.0.0")


def test_session_expires_after_configured_idle_window():
    assert session_is_valid({"authenticated": True, "last_seen": 100.0}, now=159.0, max_idle_seconds=60)
    assert not session_is_valid({"authenticated": True, "last_seen": 100.0}, now=160.0, max_idle_seconds=60)
    assert not session_is_valid({"authenticated": False, "last_seen": 100.0}, now=110.0, max_idle_seconds=60)


def test_database_orm_operations_require_identity_in_production(db_session, monkeypatch):
    monkeypatch.setattr(database, "settings", replace(settings, environment="production"))

    with pytest.raises(PermissionError, match="authenticated principal"):
        db_session.scalars(select(Company)).all()

    db_session.info["principal"] = "founder-admin"
    assert db_session.scalars(select(Company)).all() == []
