"""
tests/test_connection_status_subscription_gating.py

G.1 Stage 2 — subscription gating on /app/connection-status.

Parallel to tests/test_connection_status_endpoint.py (which is intentionally
untouched). That file pins the auth-only contract under the previous
_resolve_auth gating. This file pins the subscription-gated contract
under _require_active_subscription, plus the SubscriptionGatedException
global handler that emits the flat 402 schema documented for the
frontend api.js consumer.

Schema regression guard: tests (b), (c), and (f) assert response.json()
matches {"error": "subscription_required", "status": "<sub_status>"} EXACTLY.
This is load-bearing — if the schema drifts, the frontend api.js 402
handler silently falls through to default 'unknown' for every gated
request.

Standalone-runnable:
    python3 tests/test_connection_status_subscription_gating.py
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from product.settings import DEMO_EMAIL  # noqa: E402

os.environ.setdefault("STRIPE_PRICE_ID", "price_test_fixture")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer

import main
from main import SubscriptionGatedException, subscription_gated_handler
from product import auth_probe


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
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_customers():
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    auth_probe._cache.clear()
    yield
    auth_probe._cache.clear()


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _seed_customer(clerk_user_id, status="active", with_trial=True):
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


def _authed_session(csm_id):
    return {"csm_id": csm_id, "name": "Test User", "email": f"{csm_id}@example.com"}


# ── (a) active subscription reaches handler ──────────────────────────────────

def test_active_subscription_reaches_handler():
    """Customer with active subscription → 200 + probe body."""
    _seed_customer("user_active", status="active")
    fake_status = {"gmail": "ok", "calendar": "ok", "hubspot": "missing", "gainsight": "missing", "slack": "missing", "zoom": "missing"}

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("user_active"))), \
         patch("product.auth_probe.probe_all", return_value=fake_status):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == fake_status


# ── (b) canceled subscription returns 402 with flat schema ───────────────────

def test_canceled_subscription_returns_402():
    """Canceled customer → 402 with flat documented schema. Load-bearing
    schema assertion: drift here silently breaks frontend api.js parsing.
    """
    _seed_customer("user_canceled", status="canceled", with_trial=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("user_canceled"))):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 402, resp.text
    assert resp.json() == {"error": "subscription_required", "status": "canceled"}


# ── (c) past_due subscription returns 402 with status differentiator ─────────

def test_past_due_subscription_returns_402():
    """past_due customer → 402 with status="past_due". Pins the
    differentiator the frontend uses to route between billing-action
    and trial-ended UI.
    """
    _seed_customer("user_past_due", status="past_due", with_trial=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("user_past_due"))):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 402, resp.text
    assert resp.json() == {"error": "subscription_required", "status": "past_due"}


# ── (d) unauthenticated returns 401 ──────────────────────────────────────────

def test_no_auth_returns_401():
    """No session → _require_active_subscription raises HTTPException(401)."""
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)):
        resp = _client().get("/app/connection-status")
    assert resp.status_code == 401, resp.text


# ── (e) sarah_c demo bypass preserved ────────────────────────────────────────

def test_sarah_c_bypass_preserved():
    """sarah_c reaches /app/connection-status regardless of subscription
    state. This is the parallel-file replacement for the bypass assertion
    in tests/test_connection_status_endpoint.py:101-112, asserted against
    _require_active_subscription gating instead of the previous
    _resolve_auth gating.

    No Customer row is seeded — sarah_c bypass at main.py:209 short-
    circuits the DB lookup entirely.
    """
    sarah_session = {"csm_id": "sarah_c", "name": "Sarah", "email": DEMO_EMAIL}
    fake_status = {"gmail": "ok", "calendar": "ok", "hubspot": "missing", "gainsight": "missing", "slack": "missing", "zoom": "missing"}

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=sarah_session)), \
         patch("product.auth_probe.probe_all", return_value=fake_status):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == fake_status


# ── (f) global exception handler emits flat schema (contract-level) ──────────

def test_subscription_gated_handler_emits_flat_schema():
    """Direct test of the global SubscriptionGatedException handler.

    Pins the schema contract at the handler level — independent of any
    route's gating chain — so future refactors of _require_active_subscription
    cannot silently change the wire format.

    Constructs the exception, invokes the handler with a stub Request,
    asserts the JSONResponse body matches the documented schema exactly.
    """
    import asyncio
    import json
    from starlette.requests import Request as StarletteRequest

    scope = {"type": "http", "method": "GET", "path": "/dummy", "headers": []}
    stub_request = StarletteRequest(scope)
    exc = SubscriptionGatedException(status="test_status_value")

    response = asyncio.run(subscription_gated_handler(stub_request, exc))

    assert response.status_code == 402
    body = json.loads(response.body)
    assert body == {"error": "subscription_required", "status": "test_status_value"}


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
