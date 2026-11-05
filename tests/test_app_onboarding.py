"""
tests/test_app_onboarding.py

Regression guards for /app/onboarding/complete after the unauthenticated
body-controlled-identity gap fix (Stage 1 audit G-1).

Invariants this file enforces:
- Unauthenticated POST returns 401 (was 200 with side effects before the fix).
- Body.user_id is IGNORED even when set — identity comes from JWT only.
  Cross-user spoofing attempts must write under the JWT's csm_id, never
  the body field.
- Subscription gate works: canceled customers get 402.

Fixture pattern mirrors tests/test_settings_google_actions.py:
- In-memory SQLite + StaticPool for the customers table.
- monkeypatch.object(main, "_resolve_auth", AsyncMock(...)) to inject
  the verified-session stub. We patch _resolve_auth (not
  _require_active_subscription) so the subscription-gate code path
  in _require_active_subscription is exercised end-to-end.
- monkeypatch.chdir(tmp_path) to redirect filesystem writes
  (product/data/accounts/*.json) into a throwaway directory.

Standalone-runnable:
    python3 tests/test_app_onboarding.py
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer

import main


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────

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


@pytest.fixture(autouse=True)
def _redirect_cwd_to_tmp(monkeypatch, tmp_path):
    """Redirect filesystem writes into a throwaway directory. The
    handler writes product/data/accounts/{user_id}.json relative to
    CWD; this fixture makes those writes land in tmp_path.
    """
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture(autouse=True)
def _stub_side_effect_imports():
    """Stub out the digest queue, token loaders, digest runner, and
    engagement-email sender so the success-path test doesn't fire real
    background work or attempt real outbound calls (SMTP, Anthropic,
    Google/HubSpot APIs).

    main._run_digest_for_user is the BackgroundTasks fallback when
    Redis is unavailable; TestClient runs background tasks
    synchronously and would otherwise surface errors from the digest
    pipeline (missing SMTP env vars, etc.) as test failures.
    """
    with patch("product.queue.is_redis_available", return_value=False), \
         patch("product.auth_google.load_google_token", return_value=None), \
         patch("product.connectors.hubspot_mcp.load_hubspot_token", return_value=None), \
         patch("product.connectors.gainsight_mcp.load_gainsight_token", return_value=None), \
         patch("product.engagement_emails.send_engagement_email", return_value=None), \
         patch.object(main, "_run_digest_for_user", return_value=None):
        yield


client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _seed_customer(clerk_user_id="test_user", status="active", with_trial=True):
    """Seed a Customer row so _require_active_subscription passes."""
    trial_ends = datetime.now(timezone.utc) + timedelta(days=7) if with_trial else None
    s = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            trial_ends_at=trial_ends,
        )
        s.add(cust)
        s.commit()
    finally:
        s.close()


def _authed_session(csm_id="test_user"):
    """Stub session dict that _resolve_auth would return for an
    authenticated user.
    """
    return {"csm_id": csm_id, "name": "Test User", "email": f"{csm_id}@example.com"}


# ── Tests ────────────────────────────────────────────────────────────────────

def test_unauthenticated_post_returns_401():
    """The G-1 regression guard: an unauthenticated POST must return 401.

    Before the fix this returned 200 with side effects fired (filesystem
    write, DB row mutation, digest queue, engagement email send).
    """
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)):
        r = client.post("/app/onboarding/complete", json={"accounts": ["Acme"]})
    assert r.status_code == 401, r.text


def test_unauthenticated_post_with_body_user_id_still_returns_401():
    """Cross-user spoofing attempt from a logged-out attacker. The body
    field MUST NOT enable the request to bypass the auth check.
    """
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)):
        r = client.post(
            "/app/onboarding/complete",
            json={"accounts": ["Acme"], "user_id": "victim_clerk_id"},
        )
    assert r.status_code == 401, r.text


def test_authed_request_with_mismatched_body_user_id_uses_jwt_identity(tmp_path):
    """Cross-user spoofing attempt from a logged-in attacker. Even with
    a valid JWT, body.user_id pointing at a different csm_id must be
    ignored. Writes happen under the JWT-derived csm_id.
    """
    _seed_customer("attacker_id")

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("attacker_id"))):
        r = client.post(
            "/app/onboarding/complete",
            json={"accounts": ["Acme Co"], "user_id": "victim_clerk_id"},
        )

    assert r.status_code == 200, r.text

    # The file must be written under the JWT's csm_id, NOT the body field.
    cwd = Path(os.getcwd())
    attacker_file = cwd / "product" / "data" / "accounts" / "attacker_id.json"
    victim_file = cwd / "product" / "data" / "accounts" / "victim_clerk_id.json"
    assert attacker_file.exists(), (
        "Expected write under JWT csm_id 'attacker_id', not the body's user_id"
    )
    assert not victim_file.exists(), (
        f"REGRESSION: handler wrote under body.user_id='victim_clerk_id' "
        f"instead of JWT csm_id='attacker_id'"
    )


def test_authed_request_writes_under_jwt_identity():
    """Happy path: a logged-in user without any body.user_id field
    gets their accounts persisted under the JWT-derived csm_id.
    """
    _seed_customer("user_xyz")

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("user_xyz"))):
        r = client.post(
            "/app/onboarding/complete",
            json={"accounts": ["Acme Co", "Beta Inc"]},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "complete"
    assert body["accounts_saved"] == 2


def test_canceled_subscription_returns_402():
    """Subscription gate: a canceled customer's POST must return 402
    even with a valid JWT. Confirms _require_active_subscription is
    in the chain, not just _require_jwt_identity / _resolve_auth.
    """
    _seed_customer("canceled_user", status="canceled", with_trial=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("canceled_user"))):
        r = client.post("/app/onboarding/complete", json={"accounts": ["Acme"]})

    assert r.status_code == 402, r.text


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
