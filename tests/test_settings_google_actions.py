"""
tests/test_settings_google_actions.py

Regression guards for the Google-actions toggle endpoints and the
SCOPES_WRITE re-auth route param. After the April 23 scope hygiene
commit, gmail.send is the sole write-scope gate (calendar.events
removed from SCOPES_WRITE — feature not built, will re-add via
supplementary OAuth verification).

Scope of this file: endpoint behavior (GET/POST /app/settings/google-actions)
and the /auth/google/start?scope=write redirect path. The scope-set
shape contract (what goes inside SCOPES_WRITE) is tested separately in
tests/test_auth_google.py — sister file, distinct concern.

Coverage:
  GET /app/settings/google-actions
    - State shape when neither enabled nor scopes granted
    - State shape when both set
    - State shape when scopes granted but toggle off (realistic
      intermediate state between re-auth and toggle-on)
    - Single-scope gate: gmail.send present → scope_granted true
    - Single-scope gate: gmail.send absent → scope_granted false
  POST /app/settings/google-actions
    - Enable without gmail.send → 409 with populated auth_url
    - Enable with gmail.send → 200, DB persists
    - Disable → 200, DB persists
    - Invalid body (missing key, wrong type) → 400
  Auth / subscription gating
    - Unauthenticated → 401
    - Gated subscription → 402
    - sarah_c bypass (echo without DB write)
  /auth/google/start scope param
    - scope=write → redirect with gmail.send
    - scope=invalid → 400
    - no scope → readonly (regression, no gmail.send leak)

Fixture blend:
  - In-memory SQLite engine + monkey-patched product.database.SessionLocal
    with StaticPool (required for TestClient + DB-row tests — default
    SQLite gives each connection its own private DB, hiding seeded rows
    from the handler).
  - TestClient(main.app) for endpoint invocation.
  - patch.object on main._resolve_auth (AsyncMock) to inject a session
    stub and on product.auth_google.load_google_token to control
    scope_granted state.

Standalone-runnable:
    python3 tests/test_settings_google_actions.py
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse, parse_qs

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer
from product import auth_google

import main


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────
#
# StaticPool + check_same_thread=False is required because TestClient's
# async handler dispatch can pick up a different connection than the
# test-body's direct session. Default sqlite in-memory gives each
# connection its own private DB, which would hide the seeded Customer
# row from the handler. StaticPool pins one shared connection so all
# sessions see the same data.
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
def _bind_session_to_this_files_engine():
    """Rebind SessionLocal before every test — per the CLAUDE.md
    invariant about test files that monkey-patch _db.SessionLocal.
    """
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_customers():
    """Truncate customers between tests."""
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

_GMAIL_SEND_URL = "https://www.googleapis.com/auth/gmail.send"
_READONLY_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def _token_with_write():
    """Token with the full SCOPES_WRITE grant — gmail.send on top of readonly.

    Structurally equivalent to _token_with_gmail_send today after the April 23
    scope hygiene commit (calendar.events removed). Kept as a distinct helper
    because it represents 'full SCOPES_WRITE grant' as a semantic concept — it
    will diverge from _token_with_gmail_send when additional write scopes are
    added back (e.g., calendar.events via supplementary verification).
    """
    return {"scopes": _READONLY_SCOPES + [_GMAIL_SEND_URL], "token": "fake"}


def _token_without_write():
    """Token with readonly only — no write scope."""
    return {"scopes": _READONLY_SCOPES, "token": "fake"}


def _token_with_gmail_send():
    """Token with gmail.send on top of readonly. Satisfies the
    single-scope write gate.
    """
    return {"scopes": _READONLY_SCOPES + [_GMAIL_SEND_URL], "token": "fake"}


def _token_without_gmail_send():
    """Token without gmail.send (has calendar.events, which we no longer
    require). Must NOT satisfy scope_granted since gmail.send is the sole
    write-scope requirement.
    """
    return {"scopes": _READONLY_SCOPES + ["https://www.googleapis.com/auth/calendar.events"], "token": "fake"}


def _seed_customer(clerk_user_id="test_user", enabled=False, status="active", with_trial=True):
    """Seed a Customer row with an active subscription so
    _require_active_subscription passes and the endpoint body runs.
    """
    trial_ends = datetime.now(timezone.utc) + timedelta(days=7) if with_trial else None
    s = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            trial_ends_at=trial_ends,
            google_actions_enabled=enabled,
        )
        s.add(cust)
        s.commit()
    finally:
        s.close()


def _authed_session(csm_id="test_user"):
    """Stub session dict that _resolve_auth would return for an
    authenticated user. Same shape as live auth_google._resolve_auth
    output (csm_id, name, email).
    """
    return {"csm_id": csm_id, "name": "Test User", "email": f"{csm_id}@example.com"}


def _count_customers(clerk_user_id):
    s = _TestSession()
    try:
        return s.query(Customer).filter_by(clerk_user_id=clerk_user_id).count()
    finally:
        s.close()


def _get_customer_enabled(clerk_user_id):
    s = _TestSession()
    try:
        cust = s.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        return cust.google_actions_enabled if cust else None
    finally:
        s.close()


# ── GET /app/settings/google-actions ─────────────────────────────────────────

def test_get_returns_false_false_for_new_customer():
    """GET with Customer(enabled=False) + token without write scopes →
    {enabled: false, scope_granted: false}.
    """
    _seed_customer(enabled=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(auth_google, "load_google_token", return_value=_token_without_write()):
        r = client.get("/app/settings/google-actions")

    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False, "scope_granted": False}
    print("✓ GET: new customer → {enabled:false, scope_granted:false}")


def test_get_returns_true_true_when_both_set():
    """GET with Customer(enabled=True) + token with full write scopes →
    {enabled: true, scope_granted: true}.
    """
    _seed_customer(enabled=True)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(auth_google, "load_google_token", return_value=_token_with_write()):
        r = client.get("/app/settings/google-actions")

    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": True, "scope_granted": True}
    print("✓ GET: enabled+scopes → {enabled:true, scope_granted:true}")


def test_get_returns_false_true_when_scope_granted_but_disabled():
    """Intermediate state after re-auth but before toggle-on:
    both write scopes granted, but user hasn't flipped the toggle yet.
    """
    _seed_customer(enabled=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(auth_google, "load_google_token", return_value=_token_with_write()):
        r = client.get("/app/settings/google-actions")

    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False, "scope_granted": True}
    print("✓ GET: scopes granted but toggle off → {enabled:false, scope_granted:true}")


def test_get_scope_granted_true_when_token_has_gmail_send():
    """Single-scope gate: user has gmail.send in their token.
    _token_has_write_scopes must return True.
    """
    _seed_customer(enabled=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(auth_google, "load_google_token", return_value=_token_with_gmail_send()):
        r = client.get("/app/settings/google-actions")

    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False, "scope_granted": True}
    print("✓ GET: gmail.send present → scope_granted true")


def test_get_scope_granted_false_when_token_missing_gmail_send():
    """Single-scope gate: user has no gmail.send (this token has
    calendar.events, which we no longer require). _token_has_write_scopes
    must return False since gmail.send is the sole write-scope requirement.
    """
    _seed_customer(enabled=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(auth_google, "load_google_token", return_value=_token_without_gmail_send()):
        r = client.get("/app/settings/google-actions")

    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False, "scope_granted": False}
    print("✓ GET: gmail.send absent → scope_granted false")


# ── POST /app/settings/google-actions ────────────────────────────────────────

def test_post_enable_without_scope_returns_409_with_auth_url():
    """Enable requested without both write scopes → 409, auth_url
    populated with the authenticated csm_id.
    """
    _seed_customer(enabled=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("test_user"))), \
         patch.object(auth_google, "load_google_token", return_value=_token_without_write()):
        r = client.post("/app/settings/google-actions", json={"enabled": True})

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"] == "scope_required"
    assert body["auth_url"] == "/auth/google/start?scope=write&user_id=test_user"
    # Must not have persisted
    assert _get_customer_enabled("test_user") is False
    print("✓ POST enable without scopes → 409, auth_url populated, no DB change")


def test_post_enable_with_scope_returns_200_and_persists():
    """Enable requested with both write scopes → 200 and DB flipped to True."""
    _seed_customer(enabled=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(auth_google, "load_google_token", return_value=_token_with_write()):
        r = client.post("/app/settings/google-actions", json={"enabled": True})

    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": True, "scope_granted": True}
    assert _get_customer_enabled("test_user") is True
    print("✓ POST enable with scopes → 200, Customer.google_actions_enabled=True")


def test_post_disable_returns_200_and_persists():
    """Disable → 200, DB flipped to False regardless of scope state."""
    _seed_customer(enabled=True)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(auth_google, "load_google_token", return_value=_token_with_write()):
        r = client.post("/app/settings/google-actions", json={"enabled": False})

    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False, "scope_granted": True}
    assert _get_customer_enabled("test_user") is False
    print("✓ POST disable → 200, Customer.google_actions_enabled=False")


def test_post_invalid_body_returns_400():
    """Missing key OR wrong type → 400 with detail naming the expected shape."""
    _seed_customer(enabled=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(auth_google, "load_google_token", return_value=_token_with_write()):
        # Missing key
        r1 = client.post("/app/settings/google-actions", json={})
        # Wrong type
        r2 = client.post("/app/settings/google-actions", json={"enabled": "yes"})

    assert r1.status_code == 400, r1.text
    assert r2.status_code == 400, r2.text
    assert "enabled" in r1.json()["detail"]
    assert "enabled" in r2.json()["detail"]
    # Must not have persisted
    assert _get_customer_enabled("test_user") is False
    print("✓ POST invalid body (missing key OR wrong type) → 400, no DB change")


# ── Auth / subscription gating ───────────────────────────────────────────────

def test_unauthenticated_returns_401():
    """_resolve_auth returns None → _require_active_subscription raises 401."""
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)):
        r = client.get("/app/settings/google-actions")
    assert r.status_code == 401, r.text
    print("✓ unauthenticated → 401")


def test_subscription_gated_returns_402():
    """Customer with canceled subscription → 402."""
    _seed_customer(status="canceled", with_trial=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())):
        r = client.get("/app/settings/google-actions")
    assert r.status_code == 402, r.text
    print("✓ canceled subscription → 402")


def test_sarah_c_bypass_returns_echo_without_db_write():
    """sarah_c: GET returns echo with scope check; POST {enabled:true}
    returns echo without writing a Customer row for sarah_c.
    """
    assert _count_customers("sarah_c") == 0

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("sarah_c"))), \
         patch.object(auth_google, "load_google_token", return_value=_token_without_write()):
        r_get = client.get("/app/settings/google-actions")
        # POST enable=True as sarah_c — echoes without scope check (documented asymmetry)
        r_post = client.post("/app/settings/google-actions", json={"enabled": True})

    assert r_get.status_code == 200
    assert r_get.json() == {"enabled": False, "scope_granted": False}
    assert r_post.status_code == 200
    assert r_post.json() == {"enabled": True, "scope_granted": False}
    # Critical: sarah_c never gets a Customer row inserted
    assert _count_customers("sarah_c") == 0
    print("✓ sarah_c bypass: GET+POST echo with zero DB writes")


# ── /auth/google/start scope param ───────────────────────────────────────────

def test_auth_start_scope_write_redirects_with_SCOPES_WRITE():
    """scope=write → 302 redirect whose Location contains gmail.send
    in the Google OAuth scope query.

    After the G-2 fix the route requires a verified Clerk JWT, so we
    mock _require_jwt_identity to return the stub csm_id. The legacy
    user_id query param is accepted but ignored.
    """
    with patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "test-client-id"}), \
         patch.object(main, "_require_jwt_identity", new=AsyncMock(return_value="test_user")):
        r = client.get("/auth/google/start", params={"scope": "write"},
                       follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers.get("location", "")
    qs = parse_qs(urlparse(loc).query)
    scope_values = qs.get("scope", [""])[0].split()
    assert _GMAIL_SEND_URL in scope_values, (
        f"gmail.send not in redirect scope: {scope_values!r}"
    )
    print("✓ /auth/google/start?scope=write: redirect includes gmail.send")


def test_auth_start_scope_invalid_returns_400():
    """scope=admin (or any non-write, non-absent value) → 400.

    JWT is mocked to the stub session so the request reaches the
    scope-validation branch.
    """
    with patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "test-client-id"}), \
         patch.object(main, "_require_jwt_identity", new=AsyncMock(return_value="test_user")):
        r = client.get("/auth/google/start", params={"scope": "admin"},
                       follow_redirects=False)
    assert r.status_code == 400, r.text
    assert "Invalid scope" in r.json()["detail"]
    print("✓ /auth/google/start?scope=admin → 400")


def test_auth_start_no_scope_preserves_readonly_behavior():
    """No scope param → 302 redirect with readonly scopes only.
    Regression guard: gmail.send must not leak into the readonly redirect
    (existing callers like OnboardingWizard).
    """
    with patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "test-client-id"}), \
         patch.object(main, "_require_jwt_identity", new=AsyncMock(return_value="test_user")):
        r = client.get("/auth/google/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers.get("location", "")
    qs = parse_qs(urlparse(loc).query)
    scope_values = qs.get("scope", [""])[0].split()
    assert _GMAIL_SEND_URL not in scope_values, (
        f"gmail.send leaked into readonly redirect scope: {scope_values!r}"
    )
    # Sanity: readonly scopes are present
    assert "https://www.googleapis.com/auth/gmail.readonly" in scope_values
    print("✓ /auth/google/start (no scope): readonly redirect, no gmail.send leak")


def main_runner():
    """Standalone runner — matches the pattern in test_gmail_send.py,
    test_signal_cache.py, and test_chat_thread_id_emission.py.
    """
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
