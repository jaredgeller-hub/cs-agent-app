"""tests/test_status_page.py

Coverage for _run_status_checks, the logic behind the public /status page.

Mirrors the in-memory SQLite + autouse rebind pattern documented in
CLAUDE.md ("test files that monkey-patch _db.SessionLocal..."). See
tests/test_oauth_state.py for the canonical reference.
"""

import uuid
from product.settings import DEMO_EMAIL  # noqa: E402
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Connection, Customer, DigestLog

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
    # Re-bind on every test so sibling test files that monkey-patch
    # _db.SessionLocal don't leak across collection ordering.
    _db.SessionLocal = _TestSession
    _reset_tables()
    yield


def _reset_tables():
    session = _TestSession()
    try:
        session.query(DigestLog).delete()
        session.query(Connection).delete()
        session.query(Customer).delete()
        session.commit()
    finally:
        session.close()


import main  # noqa: E402


class _FakeResp:
    status_code = 401


@pytest.fixture(autouse=True)
def _stub_external_checks(monkeypatch):
    """Keep the HubSpot and Stripe checks off the network."""
    import product.billing as _billing

    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(_billing, "STRIPE_SECRET_KEY", "")
    yield


def _now():
    return datetime.now(timezone.utc)


def _add_customer(clerk_user_id, email, last_login_at=None, status="active"):
    session = _TestSession()
    try:
        session.add(Customer(
            id=str(uuid.uuid4()),
            clerk_user_id=clerk_user_id,
            email=email,
            subscription_status=status,
            last_login_at=last_login_at,
        ))
        session.commit()
    finally:
        session.close()


def _add_digest(customer_id, created_at, status="success"):
    session = _TestSession()
    try:
        session.add(DigestLog(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            date=created_at.strftime("%Y-%m-%d"),
            status=status,
            created_at=created_at,
        ))
        session.commit()
    finally:
        session.close()


def _add_connection(customer_id, provider, status):
    session = _TestSession()
    try:
        session.add(Connection(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            provider=provider,
            status=status,
        ))
        session.commit()
    finally:
        session.close()


def _check(name):
    for c in main._run_status_checks():
        if c["name"] == name:
            return c
    raise AssertionError(f"no check named {name!r}")


# ── Digest pipeline ──────────────────────────────────────────────────────────

def test_digest_healthy_when_recent_success():
    _add_customer("user_a", "a@example.com", last_login_at=_now() - timedelta(hours=1))
    _add_digest("user_a", _now() - timedelta(hours=1))
    c = _check("Digest pipeline")
    assert c["status"] == "ok"
    assert c["detail"].startswith("Last run")


def test_digest_idle_when_stale_and_no_recent_sign_in():
    _add_customer("user_a", "a@example.com", last_login_at=_now() - timedelta(days=50))
    _add_digest("user_a", _now() - timedelta(days=50))
    c = _check("Digest pipeline")
    assert c["status"] == "ok"
    assert c["detail"] == "Idle: no sign-ins in the last 24h"


def test_digest_idle_when_no_runs_and_no_sign_ins():
    c = _check("Digest pipeline")
    assert c["status"] == "ok"
    assert c["detail"] == "Idle: no sign-ins in the last 24h"


def test_digest_degraded_when_sign_in_but_no_digest_followed():
    _add_customer("user_a", "a@example.com", last_login_at=_now() - timedelta(hours=2))
    _add_digest("user_a", _now() - timedelta(days=3))
    c = _check("Digest pipeline")
    assert c["status"] == "degraded"
    assert "(>24h)" in c["detail"]


def test_digest_degraded_when_sign_in_and_no_runs_on_record():
    _add_customer("user_a", "a@example.com", last_login_at=_now() - timedelta(hours=2))
    c = _check("Digest pipeline")
    assert c["status"] == "degraded"
    assert c["detail"] == "No digest runs on record"


def test_digest_down_when_latest_run_failed():
    _add_customer("user_a", "a@example.com", last_login_at=_now() - timedelta(hours=1))
    _add_digest("user_a", _now() - timedelta(hours=1), status="failed")
    c = _check("Digest pipeline")
    assert c["status"] == "down"


# ── Gmail API ────────────────────────────────────────────────────────────────

def test_gmail_no_active_connections_when_no_customers():
    c = _check("Gmail API")
    assert c["status"] == "ok"
    assert c["detail"] == "No active connections"


def test_gmail_healthy_when_all_active_connections_ok():
    _add_customer("user_a", "a@example.com")
    _add_connection("user_a", "google", "active")
    _add_connection("user_a", "gmail", "active")
    c = _check("Gmail API")
    assert c["status"] == "ok"
    assert c["detail"] == "Operational"


def test_gmail_degraded_when_active_customer_connection_expired():
    _add_customer("user_a", "a@example.com")
    _add_customer("user_b", "b@example.com", status="trialing")
    _add_connection("user_a", "google", "active")
    _add_connection("user_b", "google", "expired")
    c = _check("Gmail API")
    assert c["status"] == "degraded"
    assert c["detail"] == "1 of 2 connections need attention"


def test_gmail_ignores_demo_account():
    # The demo account's dead token must not degrade the public page.
    _add_customer("sarah_c", DEMO_EMAIL)
    _add_customer("user_demo", f" {DEMO_EMAIL.title()} ")
    _add_connection("sarah_c", "google", "expired")
    _add_connection("user_demo", "google", "expired")
    c = _check("Gmail API")
    assert c["status"] == "ok"
    assert c["detail"] == "No active connections"


def test_gmail_does_not_ignore_demo_email_lookalike():
    # Demo exclusion is an exact match; a lookalike is a real customer.
    _add_customer("user_lookalike", f"{DEMO_EMAIL}.alias")
    _add_connection("user_lookalike", "google", "expired")
    c = _check("Gmail API")
    assert c["status"] == "degraded"
    assert c["detail"] == "1 of 1 connections need attention"


def test_gmail_ignores_canceled_customers():
    _add_customer("user_a", "a@example.com")
    _add_customer("user_old", "old@example.com", status="canceled")
    _add_connection("user_a", "google", "active")
    _add_connection("user_old", "google", "expired")
    c = _check("Gmail API")
    assert c["status"] == "ok"
    assert c["detail"] == "Operational"


def test_gmail_ignores_non_google_providers():
    _add_customer("user_a", "a@example.com")
    _add_connection("user_a", "hubspot", "expired")
    c = _check("Gmail API")
    assert c["status"] == "ok"
    assert c["detail"] == "No active connections"


# ── HubSpot ──────────────────────────────────────────────────────────────────

def test_hubspot_labeled_as_reachability_ping():
    names = [c["name"] for c in main._run_status_checks()]
    assert "HubSpot API reachable" in names
    assert "HubSpot MCP" not in names
    c = _check("HubSpot API reachable")
    assert c["status"] == "ok"
    assert c["detail"] == "Ping OK"


# ── Public page hygiene ──────────────────────────────────────────────────────

_SECRET_ERR = "invalid_grant: Token has been expired or revoked. refresh_token=1//0abc"


def test_raw_errors_are_logged_not_shown(monkeypatch, caplog):
    import stripe
    import product.billing as _billing

    def _boom(*a, **k):
        raise Exception(_SECRET_ERR)

    monkeypatch.setattr(_db, "SessionLocal", _boom)
    monkeypatch.setattr(main.requests, "get", _boom)
    monkeypatch.setattr(_billing, "STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(stripe.Balance, "retrieve", _boom)

    with caplog.at_level("WARNING", logger="cs-agent"):
        checks = main._run_status_checks()

    by_name = {c["name"]: c for c in checks}
    assert by_name["Gmail API"] == {"name": "Gmail API", "status": "degraded", "detail": "Check failed"}
    assert by_name["Digest pipeline"] == {"name": "Digest pipeline", "status": "degraded", "detail": "Check failed"}
    assert by_name["HubSpot API reachable"]["detail"] == "Unreachable"
    assert by_name["Stripe"]["detail"] == "Check failed"
    for c in checks:
        assert "invalid_grant" not in c["detail"]
        assert "refresh_token" not in c["detail"]
    assert caplog.text.count("invalid_grant") >= 4


def test_raw_errors_absent_from_rendered_page(monkeypatch):
    def _boom(*a, **k):
        raise Exception(_SECRET_ERR)

    monkeypatch.setattr(_db, "SessionLocal", _boom)
    monkeypatch.setattr(main.requests, "get", _boom)
    html = main._render_status_html(main._run_status_checks(), "2026-11-04 14:12 UTC")
    assert "invalid_grant" not in html
    assert "refresh_token" not in html


def test_stripe_not_configured_is_short_word():
    c = _check("Stripe")
    assert c["status"] == "down"
    assert c["detail"] == "Not configured"
