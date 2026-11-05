"""
tests/test_org_config.py

Covers product.org_config:
- try_get_csm_config (non-raising sibling of get_csm_config; returns None on miss)
- CSMConfig dataclass
- resolve_csm_config (unified view, org_config wins over Customer row)
- list_proactive_eligible_csms (trialing | active, NOT NULL clerk_user_id; past_due excluded)

Isolation:
- product.org_config._cached_config is stubbed to _FIXTURE_CONFIG per-test.
- product.database.SessionLocal is rebound to an in-memory SQLite engine
  per-test (CLAUDE.md invariant: test files that monkey-patch SessionLocal
  must also rebind it before every test).

Usage:
    python3 -m pytest tests/test_org_config.py -v
"""

import logging
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from product.settings import DEMO_EMAIL  # noqa: E402

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import product.database as _db
from product.database import Base, Connection, Customer
import product.org_config as org_config
from product.org_config import (
    CSMConfig,
    list_all_csms_with_connections,
    list_proactive_eligible_csms,
    resolve_csm_config,
)


SARAH_CLERK_ID = "user_FixtureDemoCsm0000000000000"
REVIEWER_CLERK_ID = "user_FixtureReviewer000000000000"
CUSTOMER_ONLY_CLERK_ID = "user_customer_row_only"

_FIXTURE_CONFIG = {
    "org_name": "Pilot Org",
    "crm_type": "hubspot",
    "timezone": "America/Chicago",
    "pilot_mode": "live",
    "csms": {
        SARAH_CLERK_ID: {
            "name": "Sarah C.",
            "email": DEMO_EMAIL,
            "timezone": "America/Chicago",
            "pilot_mode": "live",
            "accounts": ["Meridian Health", "Beacon Financial"],
            "notifications": {"email": True},
        },
    },
}


# ── In-memory SQLite engine for Customer-row tests ────────────────────────────

_test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _bind_session_to_this_files_engine():
    """Rebind SessionLocal before every test (see CLAUDE.md invariant)."""
    _db.SessionLocal = _TestSession
    # Clean slate for each test
    session = _TestSession()
    try:
        session.query(Connection).delete()
        session.query(Customer).delete()
        session.commit()
    finally:
        session.close()
    yield


@pytest.fixture(autouse=True)
def _stub_cached_config():
    """Replace the module-level cache with a known dict; restore after."""
    saved = org_config._cached_config
    org_config._cached_config = _FIXTURE_CONFIG
    try:
        yield
    finally:
        org_config._cached_config = saved


def _make_customer(
    clerk_user_id: str | None,
    *,
    status: str = "trialing",
    email: str | None = None,
    name: str | None = None,
    accounts=None,
    timezone: str | None = None,
) -> None:
    session = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=email or f"{clerk_user_id or uuid.uuid4().hex}@example.com",
            name=name,
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            accounts=accounts,
            timezone=timezone,
        )
        session.add(cust)
        session.commit()
    finally:
        session.close()


# ── try_get_csm_config (existing coverage) ───────────────────────────────────

def test_returns_dict_for_registered_csm():
    cfg = org_config.try_get_csm_config(SARAH_CLERK_ID)
    assert cfg is not None
    assert cfg["name"] == "Sarah C."
    assert cfg["email"] == DEMO_EMAIL
    assert cfg["accounts"] == ["Meridian Health", "Beacon Financial"]


def test_returns_none_for_unregistered_clerk_user():
    assert org_config.try_get_csm_config("user_unknown_clerk_id") is None


def test_returns_none_for_empty_string():
    assert org_config.try_get_csm_config("") is None


def test_does_not_trim_whitespace():
    """try_get_csm_config delegates to get_csm_config and does not trim.
    Whitespace-padded ids are treated as misses (return None)."""
    assert org_config.try_get_csm_config(f" {SARAH_CLERK_ID} ") is None
    assert org_config.try_get_csm_config(f"\t{SARAH_CLERK_ID}") is None


# ── CSMConfig dataclass ──────────────────────────────────────────────────────

def test_csm_config_dataclass_creation():
    cfg = CSMConfig(
        csm_id="user_x",
        name="X",
        email="x@example.com",
        timezone="America/Chicago",
        accounts=["A", "B"],
        notifications_email=True,
        pilot_mode="live",
        source="org_config",
    )
    assert cfg.csm_id == "user_x"
    assert cfg.accounts == ["A", "B"]
    assert cfg.source == "org_config"


# ── resolve_csm_config ───────────────────────────────────────────────────────

def test_resolve_csm_config_org_config_user():
    cfg = resolve_csm_config(SARAH_CLERK_ID)
    assert cfg is not None
    assert cfg.source == "org_config"
    assert cfg.name == "Sarah C."
    assert cfg.email == DEMO_EMAIL
    assert cfg.timezone == "America/Chicago"
    assert cfg.accounts == ["Meridian Health", "Beacon Financial"]
    assert cfg.notifications_email is True
    assert cfg.pilot_mode == "live"


def test_resolve_csm_config_customer_row_user():
    _make_customer(
        CUSTOMER_ONLY_CLERK_ID,
        status="active",
        email="customer@example.com",
        name="Customer Row User",
        accounts=["Sutton Retail", "Pinnacle Group"],
        timezone="America/New_York",
    )
    cfg = resolve_csm_config(CUSTOMER_ONLY_CLERK_ID)
    assert cfg is not None
    assert cfg.source == "customer_row"
    assert cfg.name == "Customer Row User"
    assert cfg.email == "customer@example.com"
    assert cfg.timezone == "America/New_York"
    assert cfg.accounts == ["Sutton Retail", "Pinnacle Group"]
    assert cfg.notifications_email is True  # default
    assert cfg.pilot_mode == "live"  # falls back to org-level


def test_resolve_csm_config_precedence_org_config_wins():
    """When a csm_id is in BOTH org_config and the customers table,
    org_config wins. The richer per-user overrides take priority."""
    _make_customer(
        SARAH_CLERK_ID,
        status="active",
        email="different@example.com",
        name="Different Name",
        accounts=["Different Account"],
        timezone="America/New_York",
    )
    cfg = resolve_csm_config(SARAH_CLERK_ID)
    assert cfg is not None
    assert cfg.source == "org_config"
    # org_config values, NOT customer-row values
    assert cfg.name == "Sarah C."
    assert cfg.email == DEMO_EMAIL
    assert cfg.timezone == "America/Chicago"
    assert cfg.accounts == ["Meridian Health", "Beacon Financial"]


def test_resolve_csm_config_unknown_returns_none():
    assert resolve_csm_config("user_totally_unknown") is None


def test_resolve_csm_config_customer_accounts_auto_returns_empty_list():
    _make_customer(
        CUSTOMER_ONLY_CLERK_ID,
        status="trialing",
        accounts="auto",
    )
    cfg = resolve_csm_config(CUSTOMER_ONLY_CLERK_ID)
    assert cfg is not None
    assert cfg.source == "customer_row"
    assert cfg.accounts == []


def test_resolve_csm_config_warns_on_malformed_accounts_in_org_config(caplog):
    """org_config entry with a non-list, non-'auto', non-None accounts
    value must (a) return CSMConfig with accounts=[] and (b) emit a
    WARNING so Sprint 6 can triage the data integrity issue from logs."""
    malformed_clerk_id = "user_malformed_accounts"
    org_config._cached_config = {
        "org_name": "Pilot Org",
        "crm_type": "hubspot",
        "timezone": "America/Chicago",
        "pilot_mode": "live",
        "csms": {
            malformed_clerk_id: {
                "name": "Malformed User",
                "email": "malformed@example.com",
                "accounts": {"foo": "bar"},  # dict, not a list or "auto"
            },
        },
    }
    caplog.set_level(logging.WARNING, logger="cs-agent.org_config")
    cfg = resolve_csm_config(malformed_clerk_id)
    assert cfg is not None
    assert cfg.source == "org_config"
    assert cfg.accounts == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("malformed accounts shape" in r.getMessage() for r in warnings), (
        f"expected 'malformed accounts shape' WARNING; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    # Verify the extra fields needed for log-only triage
    target = next(r for r in warnings if "malformed accounts shape" in r.getMessage())
    assert target.csm_id == malformed_clerk_id
    assert target.source == "org_config"
    assert target.type == "dict"
    assert "foo" in target.value_preview


# ── list_proactive_eligible_csms ─────────────────────────────────────────────

def test_list_proactive_eligible_includes_trialing_active_excludes_past_due():
    """Only trialing + active are eligible. past_due is intentionally
    excluded: is_subscription_active() gates past_due users out of the app,
    so generating proactive alerts for them is wasted work the user can
    never act on. Keeps the eligible set in sync with the access gate."""
    _make_customer("user_trialing", status="trialing")
    _make_customer("user_active", status="active")
    _make_customer("user_past_due", status="past_due")
    result = set(list_proactive_eligible_csms())
    assert result == {"user_trialing", "user_active"}
    assert "user_past_due" not in result


def test_list_proactive_eligible_excludes_canceled_and_unpaid():
    _make_customer("user_active", status="active")
    _make_customer("user_canceled", status="canceled")
    _make_customer("user_unpaid", status="unpaid")
    _make_customer("user_none", status="none")
    result = set(list_proactive_eligible_csms())
    assert result == {"user_active"}


def test_list_proactive_eligible_excludes_null_clerk_user_id():
    _make_customer("user_active", status="active")
    _make_customer(None, status="trialing", email="nullclerk@example.com")
    result = list_proactive_eligible_csms()
    assert result == ["user_active"]


# ── list_all_csms_with_connections ──────────────────────────────────────────

def _make_connection(customer_id: str, provider: str) -> None:
    session = _TestSession()
    try:
        conn = Connection(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            provider=provider,
            token_data={"access_token": "fake"},
            status="active",
        )
        session.add(conn)
        session.commit()
    finally:
        session.close()


def _make_customer_with_internal_id(
    internal_id: str,
    clerk_user_id: str,
    *,
    status: str = "active",
) -> None:
    """Create a Customer row with a specific id (not auto-generated)."""
    session = _TestSession()
    try:
        cust = Customer(
            id=internal_id,
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
        )
        session.add(cust)
        session.commit()
    finally:
        session.close()


def test_list_all_csms_with_connections_returns_csm_with_one_connection():
    _make_connection("user_one_conn", "google")
    assert list_all_csms_with_connections() == ["user_one_conn"]


def test_list_all_csms_with_connections_dedups_multi_provider_user():
    _make_connection("user_multi", "google")
    _make_connection("user_multi", "hubspot")
    _make_connection("user_multi", "gainsight")
    assert list_all_csms_with_connections() == ["user_multi"]


def test_list_all_csms_with_connections_returns_empty_when_no_rows():
    assert list_all_csms_with_connections() == []


def test_list_all_csms_with_connections_ignores_csms_with_zero_connections():
    """A Customer row without any connections must not appear."""
    _make_customer("user_no_conns", status="active")
    _make_connection("user_has_conns", "google")
    assert list_all_csms_with_connections() == ["user_has_conns"]


def test_list_all_csms_with_connections_maps_internal_customer_id_to_clerk_id():
    """Connection.customer_id can be a Customer.id (UUID) rather than a
    Clerk user_id. The helper must resolve it to the clerk_user_id."""
    internal_id = "internal-uuid-abc-123"
    clerk_id = "user_clerk_xyz"
    _make_customer_with_internal_id(internal_id, clerk_id)
    _make_connection(internal_id, "hubspot")
    assert list_all_csms_with_connections() == [clerk_id]


def test_list_all_csms_with_connections_unfiltered_by_subscription_status():
    """A canceled user with connections still gets probed (data
    portability — they're owed accurate connection state)."""
    _make_customer("user_canceled_with_conns", status="canceled")
    _make_connection("user_canceled_with_conns", "google")
    assert list_all_csms_with_connections() == ["user_canceled_with_conns"]


def test_list_all_csms_with_connections_returns_sorted_unique():
    """Multiple CSMs come back deduped and sorted for stable iteration."""
    _make_connection("zeta_user", "google")
    _make_connection("alpha_user", "hubspot")
    _make_connection("mid_user", "google")
    _make_connection("alpha_user", "gainsight")  # dup
    assert list_all_csms_with_connections() == ["alpha_user", "mid_user", "zeta_user"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
