"""Service-level tests for product.connection_service.

Exercises get_connection_state, list_active_connections, and has_scope
against in-memory SQLite fixtures. No real Postgres, no live API calls.

Mirrors the fixture pattern from tests/test_admin_audit_snapshot_connections.py:
in-memory SQLite + StaticPool, monkey-patched _db.SessionLocal, autouse
rebind and reset fixtures per CLAUDE.md invariant.

Track L scoping doc: https://www.notion.so/3591d2f9aff081a087d5d0410fe862f2
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import Base, Connection, Customer
from product.connection_service import (
    ConnectionState,
    get_connection_state,
    has_scope,
    list_active_connections,
    verify_connection_live,
)


# ---------------------------------------------------------------------------
# Test database setup
# ---------------------------------------------------------------------------

_test_engine = create_engine(
    "sqlite:///:memory:",
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _bind_session():
    """Rebind SessionLocal before every test (CLAUDE.md invariant)."""
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_tables():
    s = _TestSession()
    try:
        s.query(Connection).delete()
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


# ---------------------------------------------------------------------------
# Token fixture constants
# ---------------------------------------------------------------------------

CALENDAR_RO_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_RO_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Far-future sentinel; Credentials.expired returns False when expiry is in the future.
FAR_FUTURE_EXPIRY = "2099-12-31T00:00:00Z"

FRESH_GOOGLE_TOKEN = {
    "token": "access_token_value",
    "refresh_token": "refresh_token_value",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "test_client_id",
    "client_secret": "test_client_secret",
    "scopes": [CALENDAR_RO_SCOPE, GMAIL_RO_SCOPE],
    "expiry": FAR_FUTURE_EXPIRY,
}

# Omitting "expiry" causes Credentials.from_authorized_user_info to default
# expiry to a past datetime, making creds.expired return True deterministically.
EXPIRED_GOOGLE_TOKEN = {
    "token": "access_token_value",
    "refresh_token": "refresh_token_value",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "test_client_id",
    "client_secret": "test_client_secret",
    "scopes": [CALENDAR_RO_SCOPE],
    # no "expiry" key intentionally
}

# Raw OAuth response shape (gmail row writer at main.py:8603).
RAW_GMAIL_TOKEN = {
    "access_token": "access_token_value",
    "refresh_token": "refresh_token_value",
    "scope": f"{CALENDAR_RO_SCOPE} {GMAIL_RO_SCOPE}",
    "expires_in": 3599,
    "token_type": "Bearer",
}

HUBSPOT_TOKEN_FRESH = {
    "access_token": "hs_access",
    "refresh_token": "hs_refresh",
    "expires_in": 1800,
    "token_type": "Bearer",
    "issued_at": int(datetime.now(timezone.utc).timestamp()),
    "expires_at": int(datetime.now(timezone.utc).timestamp()) + 1800,
}

HUBSPOT_TOKEN_EXPIRED = {
    "access_token": "hs_access",
    "refresh_token": "hs_refresh",
    "expires_in": 1800,
    "token_type": "Bearer",
    "issued_at": int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()),
    "expires_at": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
}


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_customer(*, clerk_user_id: str, customer_id: str | None = None, email: str | None = None) -> str:
    """Insert a Customer row. Returns the customer.id."""
    cust_id = customer_id or str(uuid.uuid4())
    s = _TestSession()
    try:
        s.add(Customer(
            id=cust_id,
            email=email or f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status="active",
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
        ))
        s.commit()
    finally:
        s.close()
    return cust_id


def _seed_connection(*, customer_id: str, provider: str, token_data: dict) -> None:
    """Insert a Connection row."""
    s = _TestSession()
    try:
        s.add(Connection(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            provider=provider,
            token_data=token_data,
            status="active",
            last_verified_at=datetime.now(timezone.utc),
        ))
        s.commit()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def assert_note_contains(state: ConnectionState, substring: str) -> None:
    """Assert that any diagnostic note in state contains the substring."""
    matched = any(substring in n for n in state.diagnostic_notes)
    assert matched, (
        f"expected diagnostic note containing {substring!r}, "
        f"got {state.diagnostic_notes!r}"
    )


def assert_last_verified_populated_from_seed(state: ConnectionState) -> None:
    """Sprint 6 G.2: _seed_connection writes last_verified_at=now, so the
    service must surface that value (regression check against the Stage 1
    stub which hardcoded None at lines 405-406 of connection_service.py)."""
    assert state.last_verified_at is not None, (
        "expected last_verified_at to be populated from the seeded "
        "Connection row; got None — did connection_service.py revert "
        "to the Stage 1 hardcoded-None stub?"
    )
    assert state.last_verified_age_minutes is not None
    assert state.last_verified_age_minutes >= 0


def assert_last_verified_absent(state: ConnectionState) -> None:
    """No Connection row exists (or only filesystem fallback) so no probe
    has populated last_verified_at. Service returns None honestly."""
    assert state.last_verified_at is None
    assert state.last_verified_age_minutes is None


# ---------------------------------------------------------------------------
# Test 1: get_connection_state happy path
# ---------------------------------------------------------------------------

def test_get_connection_state_google_happy_path():
    _seed_customer(clerk_user_id="user_happy")
    _seed_connection(customer_id="user_happy", provider="google", token_data=FRESH_GOOGLE_TOKEN)

    state = get_connection_state("user_happy", "google")

    assert state.is_healthy is True
    assert state.has_token is True
    assert state.token_appears_expired is False
    assert CALENDAR_RO_SCOPE in state.granted_scopes
    assert GMAIL_RO_SCOPE in state.granted_scopes
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 2: get_connection_state missing row
# ---------------------------------------------------------------------------

def test_get_connection_state_missing_row():
    _seed_customer(clerk_user_id="user_no_conn")

    with patch("product.auth_google.load_google_token", return_value=None):
        state = get_connection_state("user_no_conn", "google")

    assert state.is_healthy is False
    assert state.has_token is False
    assert state.granted_scopes == []
    assert_note_contains(state, "no row in Connection table")
    assert_last_verified_absent(state)


# ---------------------------------------------------------------------------
# Test 3a: only google row exists
# ---------------------------------------------------------------------------

def test_get_connection_state_only_google_row():
    _seed_customer(clerk_user_id="user_3a")
    _seed_connection(customer_id="user_3a", provider="google", token_data=FRESH_GOOGLE_TOKEN)

    state = get_connection_state("user_3a", "google")

    assert state.has_token is True
    assert state.is_healthy is True
    assert CALENDAR_RO_SCOPE in state.granted_scopes
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 3b: only gmail row exists (silent-write-failure case)
# ---------------------------------------------------------------------------

def test_get_connection_state_only_gmail_row():
    _seed_customer(clerk_user_id="user_3b")
    _seed_connection(customer_id="user_3b", provider="gmail", token_data=RAW_GMAIL_TOKEN)

    state = get_connection_state("user_3b", "google")

    assert state.has_token is True
    # gmail-row-only users should not be reported as disconnected
    assert state.is_healthy is True
    assert CALENDAR_RO_SCOPE in state.granted_scopes
    assert GMAIL_RO_SCOPE in state.granted_scopes
    assert_note_contains(state, "only gmail row found")
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 3c: both rows exist with identical scopes
# ---------------------------------------------------------------------------

def test_get_connection_state_both_rows_identical_scopes():
    _seed_customer(clerk_user_id="user_3c")
    google_token = dict(FRESH_GOOGLE_TOKEN, scopes=[CALENDAR_RO_SCOPE, GMAIL_RO_SCOPE])
    gmail_token = dict(RAW_GMAIL_TOKEN, scope=f"{CALENDAR_RO_SCOPE} {GMAIL_RO_SCOPE}")
    _seed_connection(customer_id="user_3c", provider="google", token_data=google_token)
    _seed_connection(customer_id="user_3c", provider="gmail", token_data=gmail_token)

    state = get_connection_state("user_3c", "google")

    assert state.has_token is True
    assert state.is_healthy is True
    assert sorted(state.granted_scopes) == sorted([CALENDAR_RO_SCOPE, GMAIL_RO_SCOPE])
    assert_note_contains(state, "merged google and gmail rows")
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 3d: both rows exist with different scopes (union expected)
# ---------------------------------------------------------------------------

def test_get_connection_state_both_rows_different_scopes_union():
    _seed_customer(clerk_user_id="user_3d")
    google_token = dict(FRESH_GOOGLE_TOKEN, scopes=[CALENDAR_RO_SCOPE])
    gmail_token = dict(RAW_GMAIL_TOKEN, scope=GMAIL_RO_SCOPE)
    _seed_connection(customer_id="user_3d", provider="google", token_data=google_token)
    _seed_connection(customer_id="user_3d", provider="gmail", token_data=gmail_token)

    state = get_connection_state("user_3d", "google")

    assert state.has_token is True
    # Union of scopes from both rows
    assert CALENDAR_RO_SCOPE in state.granted_scopes
    assert GMAIL_RO_SCOPE in state.granted_scopes
    assert_note_contains(state, "merged google and gmail rows")
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 4: non-google providers (hubspot found, gainsight missing)
# ---------------------------------------------------------------------------

def test_get_connection_state_hubspot_found_gainsight_missing():
    _seed_customer(clerk_user_id="user_4")
    _seed_connection(customer_id="user_4", provider="hubspot", token_data=HUBSPOT_TOKEN_FRESH)

    hubspot_state = get_connection_state("user_4", "hubspot")
    gainsight_state = get_connection_state("user_4", "gainsight")

    assert hubspot_state.has_token is True
    assert hubspot_state.is_healthy is True
    assert hubspot_state.granted_scopes == []  # hubspot tokens have no scope field
    assert_last_verified_populated_from_seed(hubspot_state)

    assert gainsight_state.has_token is False
    assert gainsight_state.is_healthy is False
    assert_note_contains(gainsight_state, "no row in Connection table")
    assert_last_verified_absent(gainsight_state)


# ---------------------------------------------------------------------------
# Test 5: dual-key customer_id reverse lookup
# ---------------------------------------------------------------------------

def test_get_connection_state_dual_key_customer_id_lookup():
    # Customer with id="Y" and clerk_user_id="X" (different values).
    customer_internal_id = "internal-uuid-Y"
    clerk_id = "clerk-id-X"
    _seed_customer(clerk_user_id=clerk_id, customer_id=customer_internal_id)

    # Connection written under Customer.id (not clerk_user_id).
    _seed_connection(customer_id=customer_internal_id, provider="google", token_data=FRESH_GOOGLE_TOKEN)

    # Service called with clerk_user_id should find the connection via reverse-lookup.
    state = get_connection_state(clerk_id, "google")

    assert state.has_token is True
    assert state.is_healthy is True
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 6: filesystem fallback (no Postgres rows, mocked load_google_token)
# ---------------------------------------------------------------------------

def test_get_connection_state_filesystem_fallback():
    _seed_customer(clerk_user_id="user_fs")

    with patch("product.auth_google.load_google_token", return_value=FRESH_GOOGLE_TOKEN):
        state = get_connection_state("user_fs", "google")

    assert state.has_token is True
    assert state.is_healthy is True
    assert CALENDAR_RO_SCOPE in state.granted_scopes
    assert_note_contains(state, "loaded from filesystem fallback")
    assert_last_verified_absent(state)


# ---------------------------------------------------------------------------
# Test 7a: token_appears_expired True for google with expired token
# ---------------------------------------------------------------------------

def test_get_connection_state_google_token_appears_expired():
    _seed_customer(clerk_user_id="user_7a")
    _seed_connection(customer_id="user_7a", provider="google", token_data=EXPIRED_GOOGLE_TOKEN)

    state = get_connection_state("user_7a", "google")

    assert state.has_token is True
    assert state.token_appears_expired is True
    assert state.is_healthy is False  # has_token AND not expired required
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 7b: token_appears_expired True for hubspot past expires_at
# ---------------------------------------------------------------------------

def test_get_connection_state_hubspot_token_expired():
    _seed_customer(clerk_user_id="user_7b")
    _seed_connection(customer_id="user_7b", provider="hubspot", token_data=HUBSPOT_TOKEN_EXPIRED)

    state = get_connection_state("user_7b", "hubspot")

    assert state.has_token is True
    assert state.token_appears_expired is True
    assert state.is_healthy is False
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 7c: token_appears_expired False for hubspot future expires_at
# ---------------------------------------------------------------------------

def test_get_connection_state_hubspot_token_fresh():
    _seed_customer(clerk_user_id="user_7c")
    _seed_connection(customer_id="user_7c", provider="hubspot", token_data=HUBSPOT_TOKEN_FRESH)

    state = get_connection_state("user_7c", "hubspot")

    assert state.has_token is True
    assert state.token_appears_expired is False
    assert state.is_healthy is True
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 8: list_active_connections happy path
# ---------------------------------------------------------------------------

def test_list_active_connections_returns_all_with_tokens():
    _seed_customer(clerk_user_id="user_list")
    _seed_connection(customer_id="user_list", provider="google", token_data=FRESH_GOOGLE_TOKEN)
    _seed_connection(customer_id="user_list", provider="hubspot", token_data=HUBSPOT_TOKEN_FRESH)

    states = list_active_connections("user_list")

    providers = sorted(s.provider for s in states)
    assert providers == ["google", "hubspot"]
    for s in states:
        assert s.has_token is True
        assert_last_verified_populated_from_seed(s)


# ---------------------------------------------------------------------------
# Test 9: list_active_connections empty
# ---------------------------------------------------------------------------

def test_list_active_connections_empty_for_user_with_no_rows():
    _seed_customer(clerk_user_id="user_empty")

    with patch("product.auth_google.load_google_token", return_value=None):
        states = list_active_connections("user_empty")

    assert states == []


# ---------------------------------------------------------------------------
# Test 10: has_scope exact-match semantics
# ---------------------------------------------------------------------------

def test_has_scope_exact_match_only():
    _seed_customer(clerk_user_id="user_scope")
    token = dict(FRESH_GOOGLE_TOKEN, scopes=[CALENDAR_RO_SCOPE])
    _seed_connection(customer_id="user_scope", provider="google", token_data=token)

    # Exact full URL: True
    assert has_scope("user_scope", "google", CALENDAR_RO_SCOPE) is True

    # Substring "calendar": False (exact-match contract, no substring matching)
    assert has_scope("user_scope", "google", "calendar") is False

    # Different full URL: False
    assert has_scope("user_scope", "google", CALENDAR_EVENTS_SCOPE) is False


# ---------------------------------------------------------------------------
# Test 11: has_scope nonexistent user returns False, never raises
# ---------------------------------------------------------------------------

def test_has_scope_nonexistent_user_returns_false():
    # No customer or connection seeded.
    with patch("product.auth_google.load_google_token", return_value=None):
        result = has_scope("user_does_not_exist", "google", CALENDAR_RO_SCOPE)

    assert result is False


# ---------------------------------------------------------------------------
# Test 12: encrypted gmail row decrypts and exposes scopes
# ---------------------------------------------------------------------------

def _encrypted_shape(plaintext: dict) -> dict:
    """Wrap a plaintext token dict in the Stage 2a encrypted shape."""
    from product.token_crypto import encrypt
    return {"ciphertext": encrypt(json.dumps(plaintext))}


def test_get_connection_state_encrypted_gmail_row():
    _seed_customer(clerk_user_id="user_enc_gmail")
    _seed_connection(
        customer_id="user_enc_gmail",
        provider="gmail",
        token_data=_encrypted_shape(RAW_GMAIL_TOKEN),
    )

    state = get_connection_state("user_enc_gmail", "google")

    assert state.has_token is True
    assert state.is_healthy is True
    assert CALENDAR_RO_SCOPE in state.granted_scopes
    assert GMAIL_RO_SCOPE in state.granted_scopes
    assert_note_contains(state, "only gmail row found")
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Test 13: encrypted google + encrypted gmail rows merge scopes
# ---------------------------------------------------------------------------

def test_get_connection_state_encrypted_google_and_gmail_merge():
    _seed_customer(clerk_user_id="user_enc_merge")
    google_token = dict(FRESH_GOOGLE_TOKEN, scopes=[CALENDAR_RO_SCOPE])
    gmail_token = dict(RAW_GMAIL_TOKEN, scope=GMAIL_RO_SCOPE)
    _seed_connection(
        customer_id="user_enc_merge",
        provider="google",
        token_data=_encrypted_shape(google_token),
    )
    _seed_connection(
        customer_id="user_enc_merge",
        provider="gmail",
        token_data=_encrypted_shape(gmail_token),
    )

    state = get_connection_state("user_enc_merge", "google")

    assert state.has_token is True
    assert state.is_healthy is True
    assert CALENDAR_RO_SCOPE in state.granted_scopes
    assert GMAIL_RO_SCOPE in state.granted_scopes
    assert_note_contains(state, "merged google and gmail rows")
    assert_last_verified_populated_from_seed(state)


# ---------------------------------------------------------------------------
# Sprint 6 G.2 — verify_connection_live
# ---------------------------------------------------------------------------


def _seed_connection_with_state(
    *, customer_id: str, provider: str, status: str,
    last_verified_at: datetime | None,
) -> None:
    s = _TestSession()
    try:
        s.add(Connection(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            provider=provider,
            token_data={"access_token": "fake"},
            status=status,
            last_verified_at=last_verified_at,
        ))
        s.commit()
    finally:
        s.close()


def test_verify_connection_live_active_fresh_probe():
    _seed_customer(clerk_user_id="user_vcl_active")
    just_now = datetime.now(timezone.utc)
    _seed_connection_with_state(
        customer_id="user_vcl_active", provider="hubspot",
        status="active", last_verified_at=just_now,
    )

    result = verify_connection_live("user_vcl_active", "hubspot")

    assert result["provider"] == "hubspot"
    assert result["status"] == "active"
    assert result["is_live"] is True
    assert result["last_verified_at"] is not None
    assert result["last_verified_age_minutes"] == 0


def test_verify_connection_live_expired():
    _seed_customer(clerk_user_id="user_vcl_expired")
    _seed_connection_with_state(
        customer_id="user_vcl_expired", provider="gainsight",
        status="expired",
        last_verified_at=datetime.now(timezone.utc),
    )

    result = verify_connection_live("user_vcl_expired", "gainsight")

    assert result["status"] == "expired"
    assert result["is_live"] is False


def test_verify_connection_live_missing_when_no_row():
    _seed_customer(clerk_user_id="user_vcl_missing")

    result = verify_connection_live("user_vcl_missing", "hubspot")

    assert result == {
        "provider": "hubspot",
        "status": "missing",
        "last_verified_at": None,
        "last_verified_age_minutes": None,
        "is_live": False,
    }


def test_verify_connection_live_computes_age_minutes():
    _seed_customer(clerk_user_id="user_vcl_age")
    forty_min_ago = datetime.now(timezone.utc) - timedelta(minutes=40)
    _seed_connection_with_state(
        customer_id="user_vcl_age", provider="hubspot",
        status="active", last_verified_at=forty_min_ago,
    )

    result = verify_connection_live("user_vcl_age", "hubspot")

    assert result["last_verified_age_minutes"] in (39, 40, 41)
    assert result["is_live"] is True


def test_verify_connection_live_google_family_picks_freshest_across_rows():
    """For provider in {gmail, calendar, google}, look across {google,
    gmail, calendar} rows and report the freshest."""
    _seed_customer(clerk_user_id="user_vcl_family")
    older = datetime.now(timezone.utc) - timedelta(minutes=120)
    newer = datetime.now(timezone.utc) - timedelta(minutes=5)
    _seed_connection_with_state(
        customer_id="user_vcl_family", provider="gmail",
        status="active", last_verified_at=older,
    )
    _seed_connection_with_state(
        customer_id="user_vcl_family", provider="google",
        status="expired", last_verified_at=newer,
    )

    result = verify_connection_live("user_vcl_family", "gmail")

    # Freshest row wins; status from the newer "google" row
    assert result["status"] == "expired"
    assert result["last_verified_age_minutes"] in (4, 5, 6)


def test_verify_connection_live_resolves_internal_customer_id():
    """When Connection.customer_id is the Customer.id (UUID), the helper
    must reverse-lookup via Customer.clerk_user_id."""
    internal_id = "internal-uuid-vcl"
    clerk_id = "user_vcl_internal"
    _seed_customer(clerk_user_id=clerk_id, customer_id=internal_id)
    _seed_connection_with_state(
        customer_id=internal_id, provider="hubspot",
        status="active", last_verified_at=datetime.now(timezone.utc),
    )

    result = verify_connection_live(clerk_id, "hubspot")

    assert result["status"] == "active"


def test_verify_connection_live_no_session_returns_missing():
    """If SessionLocal is None (database unavailable), return missing
    sentinel rather than raising."""
    original = _db.SessionLocal
    try:
        _db.SessionLocal = None
        result = verify_connection_live("anyone", "hubspot")
        assert result["status"] == "missing"
        assert result["is_live"] is False
    finally:
        _db.SessionLocal = original


# ---------------------------------------------------------------------------
# Standalone runner (mirrors tests/test_actions_execute_email_draft.py pattern)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
