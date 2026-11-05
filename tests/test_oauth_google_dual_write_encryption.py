"""
tests/test_oauth_google_dual_write_encryption.py

Regression guard for the CASA Q44 plaintext-token gap.

The Google OAuth callback (main.py:/auth/google/callback) double-writes:
  - provider="google"  via save_google_token -> token_store.save_token  (encrypted)
  - provider="gmail"   shadow row for connection_service merge resilience

Until the May 21 fix, the gmail shadow row went through database.save_connection,
which wrote token_data in PLAINTEXT — bypassing Fernet entirely and re-creating a
plaintext row on every Google (re)auth, even after the backfill ran. The fix routes
the gmail shadow write through token_store.save_token so BOTH rows are encrypted.

These tests drive the real callback end-to-end (only the state lookup and the code
exchange are mocked) and assert that BOTH rows land as {"ciphertext": "gAAAAA..."}
and that the gmail row decrypts back to the original token fields. If anyone reverts
the gmail writer to a plaintext path, test_gmail_shadow_row_is_encrypted fails.

In-memory SQLite, autouse SessionLocal rebind per the CLAUDE.md invariant.

Usage:
    python3 tests/test_oauth_google_dual_write_encryption.py
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Connection


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────
#
# StaticPool + check_same_thread=False so the single in-memory database is
# shared across threads. TestClient dispatches the request handler on a
# separate thread, and a default :memory: engine would hand that thread its
# own empty DB ("no such table: connections"). StaticPool pins one shared
# connection so create_all() and the request-thread writes hit the same DB.
_test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession

import main  # noqa: E402  (import after SessionLocal rebind)
from product import token_store  # noqa: E402

client = TestClient(main.app)

_USER = "user_casa_q44"


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


def _raw_row(provider: str) -> dict | None:
    """Return the raw token_data dict straight from the DB (no decryption)."""
    session = _TestSession()
    try:
        conn = session.query(Connection).filter_by(
            customer_id=_USER, provider=provider
        ).first()
        return dict(conn.token_data) if conn and conn.token_data else None
    finally:
        session.close()


# A realistic Google token-exchange response. Note "scope" (space-delimited
# string) — the shape connection_service._extract_scopes_from_token reads for
# the gmail row, so it must survive the encrypt/decrypt round-trip.
_FAKE_GOOGLE_TOKEN = {
    "access_token": "ya29.test_access_token",
    "refresh_token": "1//test_refresh_token",
    "scope": "https://www.googleapis.com/auth/gmail.readonly "
             "https://www.googleapis.com/auth/calendar.readonly",
    "token_type": "Bearer",
    "expires_in": 3599,
}


def _run_callback():
    """Hit /auth/google/callback with a valid mocked state + code exchange.

    Leaves save_google_token and the gmail save_token call REAL so the writes
    actually hit the in-memory SQLite connections table.
    """
    fake_entry = {"csm_id": _USER, "code_verifier": "v", "provider": "google"}
    with patch.object(main, "_get_oauth_state_with_status",
                      return_value=(fake_entry, "ok")), \
         patch.object(main, "exchange_google_code",
                      return_value=dict(_FAKE_GOOGLE_TOKEN)):
        resp = client.get(
            "/auth/google/callback",
            params={"code": "fake_code", "state": "valid_state"},
        )
    assert resp.status_code == 200, resp.text
    assert "oauth_complete" in resp.text
    return resp


def test_both_rows_written():
    """The callback persists exactly the google + gmail row pair."""
    _run_callback()
    assert _raw_row("google") is not None, "google row missing"
    assert _raw_row("gmail") is not None, "gmail shadow row missing"


def test_google_row_is_encrypted():
    """The google row is stored as Fernet ciphertext, never plaintext."""
    _run_callback()
    raw = _raw_row("google")
    assert set(raw.keys()) == {"ciphertext"}, f"unexpected keys: {raw.keys()}"
    assert raw["ciphertext"].startswith("gAAAAA")


def test_gmail_shadow_row_is_encrypted():
    """The gmail shadow row is Fernet-encrypted — the core CASA Q44 regression.

    This is the assertion that fails if the gmail writer ever regresses to a
    plaintext path (e.g. database.save_connection).
    """
    _run_callback()
    raw = _raw_row("gmail")
    assert set(raw.keys()) == {"ciphertext"}, (
        f"gmail row is not encrypted — keys: {raw.keys()}"
    )
    assert raw["ciphertext"].startswith("gAAAAA")
    # The raw access/refresh secrets must not appear anywhere in the stored row.
    assert "ya29.test_access_token" not in json.dumps(raw)
    assert "1//test_refresh_token" not in json.dumps(raw)


def test_gmail_shadow_row_decrypts_to_token_fields():
    """Decrypting the gmail row yields the original OAuth token shape."""
    _run_callback()
    decrypted = token_store.load_token(_USER, "gmail")
    assert decrypted is not None
    assert decrypted["access_token"] == "ya29.test_access_token"
    assert decrypted["refresh_token"] == "1//test_refresh_token"
    # The space-delimited "scope" string is preserved for the merge logic.
    assert "gmail.readonly" in decrypted["scope"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
