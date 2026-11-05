"""
tests/test_token_store.py

Verifies the encryption + dual-read contract added in Stage 2a:
  - save_token() persists {"ciphertext": "gAAAAA..."} only
  - save_token() raises on encrypt failure and writes no row
  - load_token() decrypts encrypted rows transparently
  - load_token() returns legacy plaintext rows as-is and logs the fallback
  - load_token() returns None + ERROR log on key mismatch
  - load_token() returns None + ERROR log on malformed ciphertext
  - load_token() routes {"ciphertext", "extra"} as legacy (shape isn't exactly {"ciphertext"})

In-memory SQLite, autouse rebind per the CLAUDE.md invariant about
test files that monkey-patch _db.SessionLocal.

Usage:
    python3 tests/test_token_store.py
"""

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import product.database as _db
from product.database import Base, Connection


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────

_test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _bind_session_to_this_files_engine():
    """Rebind SessionLocal before every test — see CLAUDE.md invariant."""
    _db.SessionLocal = _TestSession
    _reset_table()
    yield


def _reset_table():
    session = _TestSession()
    try:
        session.query(Connection).delete()
        session.commit()
    finally:
        session.close()


def _raw_row(csm_id: str, provider: str) -> dict | None:
    """Return the raw token_data dict from Postgres without going through load_token."""
    session = _TestSession()
    try:
        conn = session.query(Connection).filter_by(
            customer_id=csm_id, provider=provider
        ).first()
        return dict(conn.token_data) if conn and conn.token_data else None
    finally:
        session.close()


def _insert_raw(csm_id: str, provider: str, token_data: dict) -> None:
    """Insert a row directly, bypassing token_store.save_token (no encryption)."""
    session = _TestSession()
    try:
        session.add(Connection(
            id=str(uuid.uuid4()),
            customer_id=csm_id,
            provider=provider,
            token_data=token_data,
            status="active",
            last_verified_at=datetime.now(timezone.utc),
        ))
        session.commit()
    finally:
        session.close()


# Import token_store AFTER SessionLocal is patched. token_crypto reads the
# env var set by conftest.py at module load.
from product import token_store  # noqa: E402
from product import token_crypto  # noqa: E402


# ── Tests ────────────────────────────────────────────────────────────────────

def test_save_token_encrypts_data():
    """save_token writes only {"ciphertext": "gAAAAA..."} — never plaintext."""
    payload = {"access_token": "secret_value", "refresh_token": "secret_refresh"}
    token_store.save_token("user_1", "google", payload)

    raw = _raw_row("user_1", "google")
    assert raw is not None
    assert set(raw.keys()) == {"ciphertext"}, f"unexpected keys: {raw.keys()}"
    assert raw["ciphertext"].startswith("gAAAAA")
    # Plaintext must not leak into the row anywhere
    assert "secret_value" not in json.dumps(raw)
    assert "secret_refresh" not in json.dumps(raw)


def test_save_token_raises_on_encrypt_failure(monkeypatch):
    """If encrypt() raises, save_token re-raises AND writes no row."""
    def boom(_plaintext):
        raise RuntimeError("encrypt boom")

    monkeypatch.setattr("product.token_crypto.encrypt", boom)
    with pytest.raises(RuntimeError, match="encrypt boom"):
        token_store.save_token("user_fail", "google", {"x": 1})

    assert _raw_row("user_fail", "google") is None


def test_load_token_decrypts_encrypted():
    """An encrypted row round-trips back to the original dict via load_token."""
    payload = {"access_token": "abc", "refresh_token": "def", "scopes": ["a", "b"]}
    token_store.save_token("user_rt", "google", payload)
    out = token_store.load_token("user_rt", "google")
    assert out == payload


def test_load_token_legacy_plaintext_fallback(caplog):
    """A legacy plaintext row is returned as-is + emits the dual-read log."""
    legacy = {
        "access_token": "legacy_access",
        "refresh_token": "legacy_refresh",
        "scopes": ["openid"],
    }
    _insert_raw("user_legacy", "google", legacy)

    caplog.set_level(logging.INFO, logger="cs-agent.token_store")
    out = token_store.load_token("user_legacy", "google")
    assert out == legacy

    messages = [r.getMessage() for r in caplog.records]
    assert any("token_decrypt_legacy_fallback" in m for m in messages), messages


def test_load_token_key_mismatch_returns_none(caplog, monkeypatch):
    """A ciphertext encrypted with a different key returns None + ERROR log."""
    other_fernet = Fernet(Fernet.generate_key())
    foreign_ciphertext = other_fernet.encrypt(b'{"x": 1}').decode("ascii")
    _insert_raw("user_mismatch", "google", {"ciphertext": foreign_ciphertext})

    caplog.set_level(logging.ERROR, logger="cs-agent.token_store")
    out = token_store.load_token("user_mismatch", "google")
    assert out is None

    messages = [r.getMessage() for r in caplog.records]
    assert any("token_decrypt_key_mismatch" in m for m in messages), messages


def test_load_token_malformed_ciphertext_returns_none(caplog):
    """A non-Fernet 'ciphertext' value emits format_invalid and returns None."""
    _insert_raw("user_malformed", "google", {"ciphertext": "not_a_real_fernet_token"})

    caplog.set_level(logging.ERROR, logger="cs-agent.token_store")
    out = token_store.load_token("user_malformed", "google")
    assert out is None

    messages = [r.getMessage() for r in caplog.records]
    assert any("token_decrypt_format_invalid" in m for m in messages), messages


def test_load_token_handles_dict_with_extra_keys_as_legacy(caplog):
    """{"ciphertext": "...", "extra_field": "..."} is NOT the encrypted shape.

    Encrypted shape is exactly {"ciphertext"} — one key, no more. Anything
    else routes through the legacy fallback path so we don't accidentally
    treat a malformed dual-key payload as a corrupt encrypted row.
    """
    weird = {"ciphertext": "gAAAAA_pretend_value", "extra_field": "something"}
    _insert_raw("user_extra", "google", weird)

    caplog.set_level(logging.INFO, logger="cs-agent.token_store")
    out = token_store.load_token("user_extra", "google")
    assert out == weird

    messages = [r.getMessage() for r in caplog.records]
    assert any("token_decrypt_legacy_fallback" in m for m in messages), messages


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
