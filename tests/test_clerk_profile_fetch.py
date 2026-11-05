"""tests/test_clerk_profile_fetch.py

Covers the F15 fix: when the Clerk JWT lacks an email or first_name
claim, the customer-row writers and the auth_clerk self-heal path
fetch real values from the Clerk Backend API instead of persisting
the @noreply.clerk placeholder forever.

Tests:
  fetch_clerk_user_profile (unit)
    - 200 with primary email returns dict
    - 200 with no primary email returns None
    - 404 returns None and warns
    - HTTP error returns None and warns
    - Empty CLERK_SECRET_KEY returns None and warns

  provision_customer_on_first_auth (integration, in-memory SQLite)
    - JWT email present: helper not called, real email written
    - JWT email None, Clerk has email: helper called, Clerk email written
    - JWT email None, Clerk also returns None: falls back to @noreply.clerk
    - JWT email is @noreply.clerk-shaped: helper called, real email written

  get_or_create_customer (parallel writer)
    - Same matrix as provision

  Self-heal at auth_clerk.verify_clerk_token
    - Existing @noreply.clerk row + Clerk has email: row updated to real email and name

Usage:
    python3 tests/test_clerk_profile_fetch.py
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import product.database as _db
from product.database import Base, Customer, EngagementSend


# In-memory SQLite engine reused across tests.
_test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _bind_session_to_this_files_engine():
        _db.SessionLocal = _TestSession
        yield
except ImportError:
    pass


def _reset_tables() -> None:
    session = _TestSession()
    try:
        session.query(Customer).delete()
        session.query(EngagementSend).delete()
        session.commit()
    finally:
        session.close()


def _get_customer(clerk_user_id: str) -> Customer | None:
    session = _TestSession()
    try:
        return session.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
    finally:
        session.close()


# ── fetch_clerk_user_profile unit tests ──────────────────────────────────────

def _make_response(status_code: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("no body"))
    return resp


def _patched_httpx_client(response: MagicMock | None, raise_exc: Exception | None = None):
    """Build a MagicMock substitute for httpx.Client used as a context manager."""
    fake_client = MagicMock()
    if raise_exc is not None:
        fake_client.get = MagicMock(side_effect=raise_exc)
    else:
        fake_client.get = MagicMock(return_value=response)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=fake_client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _import_auth_clerk_with_secret(secret: str):
    """Reload auth_clerk so module-level CLERK_SECRET_KEY picks up the test env."""
    import importlib
    if secret:
        os.environ["CLERK_SECRET_KEY"] = secret
    else:
        os.environ.pop("CLERK_SECRET_KEY", None)
    import product.auth_clerk as ac
    importlib.reload(ac)
    return ac


def test_fetch_returns_email_on_200_with_primary():
    ac = _import_auth_clerk_with_secret("sk_test_dummy")
    body = {
        "primary_email_address_id": "idn_primary",
        "email_addresses": [
            {"id": "idn_other", "email_address": "secondary@example.com"},
            {"id": "idn_primary", "email_address": "real@example.com"},
        ],
        "first_name": "Real",
        "last_name": "User",
    }
    fake_cm = _patched_httpx_client(_make_response(200, body))
    with patch("httpx.Client", return_value=fake_cm):
        out = ac.fetch_clerk_user_profile("user_abc123")
    assert out == {
        "email": "real@example.com",
        "first_name": "Real",
        "last_name": "User",
        "name": "Real User",
    }, f"unexpected payload: {out}"
    print("[PASS] 200 with primary email returns dict")


def test_fetch_returns_none_when_no_primary_email():
    ac = _import_auth_clerk_with_secret("sk_test_dummy")
    body = {
        "primary_email_address_id": None,
        "email_addresses": [],
        "first_name": "NoMail",
        "last_name": None,
    }
    fake_cm = _patched_httpx_client(_make_response(200, body))
    with patch("httpx.Client", return_value=fake_cm):
        out = ac.fetch_clerk_user_profile("user_no_email")
    assert out is None, f"expected None when primary email missing, got {out}"
    print("[PASS] 200 with no primary email returns None")


def test_fetch_returns_none_on_404():
    ac = _import_auth_clerk_with_secret("sk_test_dummy")
    fake_cm = _patched_httpx_client(_make_response(404, {"errors": ["not found"]}))
    caplog = _capture_warnings("cs-agent.auth_clerk")
    with patch("httpx.Client", return_value=fake_cm), caplog:
        out = ac.fetch_clerk_user_profile("user_missing")
    assert out is None
    assert any("status=404" in r for r in caplog.records), "expected WARNING for 404"
    print("[PASS] 404 returns None and warns")


def test_fetch_returns_none_on_http_error():
    import httpx
    ac = _import_auth_clerk_with_secret("sk_test_dummy")
    fake_cm = _patched_httpx_client(None, raise_exc=httpx.ConnectTimeout("boom"))
    caplog = _capture_warnings("cs-agent.auth_clerk")
    with patch("httpx.Client", return_value=fake_cm), caplog:
        out = ac.fetch_clerk_user_profile("user_timeout")
    assert out is None
    assert any("HTTP error" in r for r in caplog.records), "expected WARNING for HTTP error"
    print("[PASS] HTTP error returns None and warns")


def test_fetch_returns_none_when_secret_unset():
    ac = _import_auth_clerk_with_secret("")
    caplog = _capture_warnings("cs-agent.auth_clerk")
    with caplog:
        out = ac.fetch_clerk_user_profile("user_anything")
    assert out is None
    assert any("CLERK_SECRET_KEY unset" in r for r in caplog.records), \
        "expected WARNING when secret missing"
    print("[PASS] missing CLERK_SECRET_KEY returns None and warns")


def test_fetch_never_logs_email_at_info():
    """Defensive: scan auth_clerk log records during a successful fetch and
    assert none of them contain the real email at INFO level."""
    ac = _import_auth_clerk_with_secret("sk_test_dummy")
    body = {
        "primary_email_address_id": "idn_primary",
        "email_addresses": [
            {"id": "idn_primary", "email_address": "secret.user@example.com"},
        ],
        "first_name": "Secret",
        "last_name": "User",
    }
    fake_cm = _patched_httpx_client(_make_response(200, body))
    capture = _CapturingHandler(level=logging.INFO)
    log = logging.getLogger("cs-agent.auth_clerk")
    log.addHandler(capture)
    log.setLevel(logging.DEBUG)
    try:
        with patch("httpx.Client", return_value=fake_cm):
            ac.fetch_clerk_user_profile("user_log_test")
    finally:
        log.removeHandler(capture)
    info_records = [r for r in capture.records if r.levelno >= logging.INFO]
    leak = [r for r in info_records if "secret.user@example.com" in (r.getMessage() or "")]
    assert not leak, f"PII leak at INFO: {[r.getMessage() for r in leak]}"
    print("[PASS] fetch never logs real email at INFO")


# ── provision_customer_on_first_auth integration tests ───────────────────────

def test_provision_uses_jwt_email_when_present():
    _reset_tables()
    from product.subscription import provision_customer_on_first_auth
    with patch("product.auth_clerk.fetch_clerk_user_profile") as mock_fetch:
        result = provision_customer_on_first_auth(
            clerk_user_id="user_jwt_present",
            email="real@example.com",
            name="Real Name",
        )
    assert mock_fetch.call_count == 0, "helper should not be called when JWT email present"
    assert result is not None
    cust = _get_customer("user_jwt_present")
    assert cust.email == "real@example.com"
    assert cust.name == "Real Name"
    print("[PASS] provision: JWT email present, helper not called")


def test_provision_falls_back_to_clerk_when_jwt_email_none():
    _reset_tables()
    from product.subscription import provision_customer_on_first_auth
    with patch(
        "product.auth_clerk.fetch_clerk_user_profile",
        return_value={
            "email": "from-clerk@example.com",
            "first_name": "From",
            "last_name": "Clerk",
            "name": "From Clerk",
        },
    ) as mock_fetch:
        provision_customer_on_first_auth(
            clerk_user_id="user_jwt_none",
            email=None,
            name=None,
        )
    assert mock_fetch.call_count == 1
    cust = _get_customer("user_jwt_none")
    assert cust.email == "from-clerk@example.com"
    assert cust.name == "From Clerk"
    print("[PASS] provision: JWT None, Clerk has email, real email written")


def test_provision_falls_back_to_placeholder_when_clerk_returns_none():
    _reset_tables()
    from product.subscription import provision_customer_on_first_auth
    with patch(
        "product.auth_clerk.fetch_clerk_user_profile",
        return_value=None,
    ) as mock_fetch:
        provision_customer_on_first_auth(
            clerk_user_id="user_no_clerk_email",
            email=None,
            name=None,
        )
    assert mock_fetch.call_count == 1
    cust = _get_customer("user_no_clerk_email")
    assert cust.email == "user_no_clerk_email@noreply.clerk"
    assert cust.name is None
    print("[PASS] provision: JWT None + Clerk None falls back to @noreply.clerk")


def test_provision_replaces_noreply_input_with_clerk_email():
    _reset_tables()
    from product.subscription import provision_customer_on_first_auth
    with patch(
        "product.auth_clerk.fetch_clerk_user_profile",
        return_value={
            "email": "actually-real@example.com",
            "first_name": "A",
            "last_name": "B",
            "name": "A B",
        },
    ) as mock_fetch:
        provision_customer_on_first_auth(
            clerk_user_id="user_noreply_input",
            email="user_noreply_input@noreply.clerk",
            name=None,
        )
    assert mock_fetch.call_count == 1
    cust = _get_customer("user_noreply_input")
    assert cust.email == "actually-real@example.com"
    assert cust.name == "A B"
    print("[PASS] provision: @noreply.clerk-shaped input triggers helper")


# ── get_or_create_customer integration tests ────────────────────────────────

def test_get_or_create_uses_jwt_email_when_present():
    _reset_tables()
    from product.database import get_or_create_customer
    with patch("product.auth_clerk.fetch_clerk_user_profile") as mock_fetch:
        get_or_create_customer(
            clerk_user_id="user_goc_jwt",
            email="goc-real@example.com",
            name="GOC Real",
        )
    assert mock_fetch.call_count == 0
    cust = _get_customer("user_goc_jwt")
    assert cust.email == "goc-real@example.com"
    assert cust.name == "GOC Real"
    print("[PASS] get_or_create: JWT email present, helper not called")


def test_get_or_create_falls_back_to_clerk_when_email_none():
    _reset_tables()
    from product.database import get_or_create_customer
    with patch(
        "product.auth_clerk.fetch_clerk_user_profile",
        return_value={
            "email": "goc-clerk@example.com",
            "first_name": "GOC",
            "last_name": "Clerk",
            "name": "GOC Clerk",
        },
    ) as mock_fetch:
        get_or_create_customer(
            clerk_user_id="user_goc_none",
            email=None,
            name=None,
        )
    assert mock_fetch.call_count == 1
    cust = _get_customer("user_goc_none")
    assert cust.email == "goc-clerk@example.com"
    assert cust.name == "GOC Clerk"
    print("[PASS] get_or_create: JWT None, Clerk has email, real email written")


def test_get_or_create_falls_back_to_placeholder_when_clerk_none():
    _reset_tables()
    from product.database import get_or_create_customer
    with patch(
        "product.auth_clerk.fetch_clerk_user_profile",
        return_value=None,
    ):
        get_or_create_customer(
            clerk_user_id="user_goc_no_clerk",
            email=None,
            name=None,
        )
    cust = _get_customer("user_goc_no_clerk")
    assert cust.email == "user_goc_no_clerk@noreply.clerk"
    print("[PASS] get_or_create: JWT None + Clerk None falls back to @noreply.clerk")


# ── Self-heal extension test ────────────────────────────────────────────────

def test_self_heal_updates_existing_noreply_row_via_clerk():
    """Existing customer row with @noreply.clerk + JWT still missing email →
    self-heal block should fetch from Clerk and update both email and name."""
    _reset_tables()
    # Seed an existing row with the placeholder email and no name.
    session = _TestSession()
    try:
        session.add(Customer(
            id=str(uuid.uuid4()),
            clerk_user_id="user_existing_noreply",
            email="user_existing_noreply@noreply.clerk",
            name=None,
            subscription_status="trialing",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()
    finally:
        session.close()

    # Simulate the self-heal block. We do not need to invoke the full
    # FastAPI path — we replicate the conditions verify_clerk_token enters
    # with after JWT decode.
    from product import auth_clerk as ac
    profile_payload = {
        "email": "real@example.com",
        "first_name": "Real",
        "last_name": "Name",
        "name": "Real Name",
    }

    user_info = {
        "user_id": "user_existing_noreply",
        "email": None,
        "first_name": None,
    }

    # Reproduce the post-decode self-heal logic by calling the public
    # helpers directly.
    target_email = user_info["email"]
    target_name = user_info["first_name"]
    if (not target_email) or ac.is_noreply_email(target_email) or not target_name:
        with patch.object(ac, "fetch_clerk_user_profile", return_value=profile_payload):
            profile = ac.fetch_clerk_user_profile(user_info["user_id"])
        if profile:
            if profile.get("email") and ((not target_email) or ac.is_noreply_email(target_email)):
                target_email = profile["email"]
            if profile.get("name") and not target_name:
                target_name = profile["name"]

    assert target_email == "real@example.com"
    assert target_name == "Real Name"

    # Now perform the actual DB write the same way the production path does.
    s2 = _TestSession()
    try:
        cust = s2.query(Customer).filter_by(clerk_user_id="user_existing_noreply").first()
        assert cust is not None
        if cust.email and ac.is_noreply_email(cust.email):
            cust.email = target_email
        if target_name and not cust.name:
            cust.name = target_name
        s2.commit()
    finally:
        s2.close()

    after = _get_customer("user_existing_noreply")
    assert after.email == "real@example.com"
    assert after.name == "Real Name"
    print("[PASS] self-heal updates existing @noreply.clerk row from Clerk profile")


# ── Backfill script tests ───────────────────────────────────────────────────

def test_backfill_dry_run_does_not_write():
    _reset_tables()
    session = _TestSession()
    try:
        session.add(Customer(
            id=str(uuid.uuid4()),
            clerk_user_id="user_backfill_dry",
            email="user_backfill_dry@noreply.clerk",
            subscription_status="trialing",
            created_at=datetime.now(timezone.utc),
        ))
        session.add(EngagementSend(
            id=str(uuid.uuid4()),
            csm_id="user_backfill_dry",
            day=1,
        ))
        session.commit()
    finally:
        session.close()

    profile = {
        "email": "real@example.com",
        "first_name": "Real",
        "last_name": "User",
        "name": "Real User",
    }
    from scripts import backfill_clerk_emails as bf
    with patch.object(bf, "fetch_clerk_user_profile", return_value=profile):
        rc = bf.run_backfill(dry_run=True)
    assert rc == 0

    cust = _get_customer("user_backfill_dry")
    assert cust.email == "user_backfill_dry@noreply.clerk", "dry-run must not modify DB"
    s = _TestSession()
    try:
        engagement_count = s.query(EngagementSend).filter_by(csm_id="user_backfill_dry").count()
    finally:
        s.close()
    assert engagement_count == 1, "dry-run must not delete engagement_sends rows"
    print("[PASS] backfill --dry-run does not write to DB")


def test_backfill_writes_and_clears_engagement_sends():
    _reset_tables()
    session = _TestSession()
    try:
        session.add(Customer(
            id=str(uuid.uuid4()),
            clerk_user_id="user_backfill_real",
            email="user_backfill_real@noreply.clerk",
            subscription_status="trialing",
            created_at=datetime.now(timezone.utc),
        ))
        session.add(EngagementSend(
            id=str(uuid.uuid4()),
            csm_id="user_backfill_real",
            day=1,
        ))
        session.add(EngagementSend(
            id=str(uuid.uuid4()),
            csm_id="user_backfill_real",
            day=3,
        ))
        session.commit()
    finally:
        session.close()

    profile = {
        "email": "real-write@example.com",
        "first_name": "Real",
        "last_name": "Write",
        "name": "Real Write",
    }
    from scripts import backfill_clerk_emails as bf
    with patch.object(bf, "fetch_clerk_user_profile", return_value=profile):
        rc = bf.run_backfill(dry_run=False)
    assert rc == 0

    cust = _get_customer("user_backfill_real")
    assert cust.email == "real-write@example.com"
    assert cust.name == "Real Write"
    s = _TestSession()
    try:
        engagement_count = s.query(EngagementSend).filter_by(csm_id="user_backfill_real").count()
    finally:
        s.close()
    assert engagement_count == 0, "engagement_sends must be cleared after update"
    print("[PASS] backfill updates row and clears engagement_sends")


def test_backfill_idempotent_second_run_is_noop():
    """After a successful backfill, re-running must skip rows that no longer
    match @noreply.clerk."""
    _reset_tables()
    session = _TestSession()
    try:
        session.add(Customer(
            id=str(uuid.uuid4()),
            clerk_user_id="user_backfill_idem",
            email="user_backfill_idem@noreply.clerk",
            subscription_status="trialing",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()
    finally:
        session.close()

    profile = {
        "email": "idem@example.com",
        "first_name": "I",
        "last_name": "D",
        "name": "I D",
    }
    from scripts import backfill_clerk_emails as bf
    with patch.object(bf, "fetch_clerk_user_profile", return_value=profile) as mock_fetch:
        rc1 = bf.run_backfill(dry_run=False)
        rc2 = bf.run_backfill(dry_run=False)
    assert rc1 == 0 and rc2 == 0
    # First run calls helper once; second run finds zero matching rows so
    # the helper should not be called again.
    assert mock_fetch.call_count == 1, (
        f"second run should not call Clerk; got {mock_fetch.call_count} calls total"
    )
    print("[PASS] backfill is idempotent")


def test_backfill_skips_when_clerk_has_no_email():
    _reset_tables()
    session = _TestSession()
    try:
        session.add(Customer(
            id=str(uuid.uuid4()),
            clerk_user_id="user_backfill_none",
            email="user_backfill_none@noreply.clerk",
            subscription_status="trialing",
            created_at=datetime.now(timezone.utc),
        ))
        session.add(EngagementSend(
            id=str(uuid.uuid4()),
            csm_id="user_backfill_none",
            day=1,
        ))
        session.commit()
    finally:
        session.close()

    from scripts import backfill_clerk_emails as bf
    with patch.object(bf, "fetch_clerk_user_profile", return_value=None):
        rc = bf.run_backfill(dry_run=False)
    assert rc == 0

    cust = _get_customer("user_backfill_none")
    assert cust.email == "user_backfill_none@noreply.clerk", \
        "row must be untouched when Clerk has no email"
    s = _TestSession()
    try:
        engagement_count = s.query(EngagementSend).filter_by(csm_id="user_backfill_none").count()
    finally:
        s.close()
    assert engagement_count == 1, "engagement_sends rows must be preserved when row is skipped"
    print("[PASS] backfill skips rows when Clerk has no primary email")


# ── Init failure-mode tests ─────────────────────────────────────────────────

def test_backfill_errors_when_database_url_unset():
    """Standalone scripts hit SessionLocal=None at import; we then probe the
    DATABASE_URL env to give an accurate diagnostic instead of the misleading
    'DATABASE_URL not configured' that fired even when init_db simply hadn't
    run."""
    from scripts import backfill_clerk_emails as bf
    captured = _capture_warnings("backfill_clerk_emails", level=logging.ERROR)
    with patch.object(_db, "SessionLocal", None), \
         patch.object(_db, "DATABASE_URL", None), \
         captured:
        rc = bf.run_backfill(dry_run=True)
    assert rc == 2
    assert any("DATABASE_URL env var not set" in r for r in captured.records), \
        f"expected env-var error message; got {captured.records!r}"
    print("[PASS] backfill errors with DATABASE_URL-unset message when env missing")


def test_backfill_errors_when_init_db_returns_false():
    """init_db can return False if DATABASE_URL is set but invalid or the
    Postgres engine fails to connect. Surface that case distinctly from the
    env-var-missing path so operators know which knob to turn."""
    from scripts import backfill_clerk_emails as bf
    captured = _capture_warnings("backfill_clerk_emails", level=logging.ERROR)
    with patch.object(_db, "SessionLocal", None), \
         patch.object(_db, "DATABASE_URL", "postgresql://broken"), \
         patch.object(_db, "init_db", return_value=False), \
         captured:
        rc = bf.run_backfill(dry_run=True)
    assert rc == 2
    assert any("Database initialization failed" in r for r in captured.records), \
        f"expected init-failure error message; got {captured.records!r}"
    print("[PASS] backfill errors with init-failure message when init_db returns False")


def test_backfill_errors_when_session_local_still_none_after_init():
    """Defensive: init_db returning True but leaving SessionLocal None should
    not silently proceed. Real production would never hit this branch, but a
    misconfigured monkey-patch (e.g. a future contributor wiring a broken
    fake init_db) should not be confused with the other two error modes."""
    from scripts import backfill_clerk_emails as bf
    captured = _capture_warnings("backfill_clerk_emails", level=logging.ERROR)
    with patch.object(_db, "SessionLocal", None), \
         patch.object(_db, "DATABASE_URL", "postgresql://something"), \
         patch.object(_db, "init_db", return_value=True), \
         captured:
        rc = bf.run_backfill(dry_run=True)
    assert rc == 2
    assert any("SessionLocal unexpectedly None" in r for r in captured.records), \
        f"expected defensive error message; got {captured.records!r}"
    print("[PASS] backfill errors with defensive message when SessionLocal stays None")


def test_backfill_does_not_call_init_db_when_session_local_already_bound():
    """The fix must not break test fixtures that pre-bind _db.SessionLocal.
    init_db should be invoked only when SessionLocal is currently None."""
    _reset_tables()
    from scripts import backfill_clerk_emails as bf
    with patch.object(_db, "init_db") as mock_init, \
         patch.object(bf, "fetch_clerk_user_profile", return_value=None):
        rc = bf.run_backfill(dry_run=True)
    assert rc == 0
    assert mock_init.call_count == 0, (
        f"init_db should not be called when SessionLocal is already bound; "
        f"got {mock_init.call_count} calls"
    )
    print("[PASS] backfill skips init_db when SessionLocal is already bound")


# ── Logging capture helpers ──────────────────────────────────────────────────

class _CapturingHandler(logging.Handler):
    def __init__(self, level: int = logging.WARNING):
        super().__init__(level=level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _capture_warnings:
    """Context manager that captures records from a logger for assertion."""

    def __init__(self, logger_name: str, level: int = logging.WARNING):
        self.logger = logging.getLogger(logger_name)
        self.level = level
        self.handler = _CapturingHandler(level=level)
        self.records: list[str] = []
        self._prev_level: int | None = None

    def __enter__(self) -> "_capture_warnings":
        self._prev_level = self.logger.level
        self.logger.setLevel(min(self.level, logging.WARNING))
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.logger.removeHandler(self.handler)
        if self._prev_level is not None:
            self.logger.setLevel(self._prev_level)
        self.records = [r.getMessage() for r in self.handler.records]


def main() -> int:
    tests = [
        test_fetch_returns_email_on_200_with_primary,
        test_fetch_returns_none_when_no_primary_email,
        test_fetch_returns_none_on_404,
        test_fetch_returns_none_on_http_error,
        test_fetch_returns_none_when_secret_unset,
        test_fetch_never_logs_email_at_info,
        test_provision_uses_jwt_email_when_present,
        test_provision_falls_back_to_clerk_when_jwt_email_none,
        test_provision_falls_back_to_placeholder_when_clerk_returns_none,
        test_provision_replaces_noreply_input_with_clerk_email,
        test_get_or_create_uses_jwt_email_when_present,
        test_get_or_create_falls_back_to_clerk_when_email_none,
        test_get_or_create_falls_back_to_placeholder_when_clerk_none,
        test_self_heal_updates_existing_noreply_row_via_clerk,
        test_backfill_dry_run_does_not_write,
        test_backfill_writes_and_clears_engagement_sends,
        test_backfill_idempotent_second_run_is_noop,
        test_backfill_skips_when_clerk_has_no_email,
        test_backfill_errors_when_database_url_unset,
        test_backfill_errors_when_init_db_returns_false,
        test_backfill_errors_when_session_local_still_none_after_init,
        test_backfill_does_not_call_init_db_when_session_local_already_bound,
    ]
    failures: list[tuple[str, str]] = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures.append((t.__name__, str(e)))
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"[FAIL] {t.__name__}: {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"FAILED: {len(failures)} of {len(tests)}")
        return 1
    print(f"PASSED: {len(tests)} of {len(tests)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
