"""
tests/test_demo_identity_exact_match.py

The demo bypass must match only the real demo address (exact,
case-insensitive, whitespace-trimmed), never a substring. Covers:

  - is_demo_email unit matrix (real address variants vs. lookalikes)
  - provisioning: subscription.provision_customer_on_first_auth skips the
    real demo address and provisions a normal trial for lookalikes
  - billing gate: database.get_or_create_customer gives the demo address
    the no-trial "none" status, and lookalikes a trialing row that passes
    is_subscription_active
  - CSM API key registry: no default key; missing/unknown keys get 401

Standalone-runnable:
    python3 tests/test_demo_identity_exact_match.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import product.database as _db
from product.database import Base, Customer
from product.demo_identity import DEMO_EMAIL, is_demo_email
from product.subscription import is_subscription_active

_test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _bind_session_to_this_files_engine():
    _db.SessionLocal = _TestSession
    yield


# Derived from the configured demo address (settings.DEMO_EMAIL) so the
# matrix holds for any deployment's value.
_LOCAL, _DOMAIN = DEMO_EMAIL.split("@", 1)

REAL_DEMO_VARIANTS = [
    DEMO_EMAIL,
    DEMO_EMAIL.upper(),
    DEMO_EMAIL.title(),
    f"  {DEMO_EMAIL}  ",
    f"\t{DEMO_EMAIL.capitalize()}\n",
]

LOOKALIKES = [
    f"{DEMO_EMAIL}.example.org",
    f"x{DEMO_EMAIL}",
    f"{DEMO_EMAIL}x",
    f"evil+{DEMO_EMAIL}",
    f"{DEMO_EMAIL}@attacker.example",
    f"not{DEMO_EMAIL}",
    DEMO_EMAIL[:-1],
    f"{_LOCAL}_x@{_DOMAIN}",
]


def _reset() -> None:
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()


def _row(clerk_user_id: str) -> Customer | None:
    s = _TestSession()
    try:
        return s.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
    finally:
        s.close()


# ── Unit ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email", REAL_DEMO_VARIANTS)
def test_real_demo_address_matches(email):
    assert is_demo_email(email) is True


@pytest.mark.parametrize("email", LOOKALIKES)
def test_lookalikes_do_not_match(email):
    assert is_demo_email(email) is False


@pytest.mark.parametrize("email", [None, "", "   ", 123])
def test_empty_or_non_string_does_not_match(email):
    assert is_demo_email(email) is False


def test_demo_email_constant_comes_from_settings():
    import product.settings as settings
    assert DEMO_EMAIL == settings.DEMO_EMAIL
    assert DEMO_EMAIL == DEMO_EMAIL.strip().lower()
    assert len(set(LOOKALIKES)) == len(LOOKALIKES)
    assert not set(LOOKALIKES) & {v.strip().lower() for v in REAL_DEMO_VARIANTS}


# ── Provisioning (subscription.provision_customer_on_first_auth) ─────────────

@pytest.mark.parametrize("email", REAL_DEMO_VARIANTS)
def test_provision_skips_real_demo_address(email):
    _reset()
    from product.subscription import provision_customer_on_first_auth
    with patch("product.auth_clerk.fetch_clerk_user_profile") as fetch:
        result = provision_customer_on_first_auth("user_demo_real", email, "Sarah")
    assert result is None
    assert fetch.call_count == 0
    assert _row("user_demo_real") is None


@pytest.mark.parametrize("email", LOOKALIKES)
def test_provision_gives_lookalike_a_normal_trial(email):
    _reset()
    from product.subscription import provision_customer_on_first_auth
    with patch("product.auth_clerk.fetch_clerk_user_profile"):
        result = provision_customer_on_first_auth("user_lookalike", email, "Look Alike")
    assert result is not None
    assert result["subscription_status"] == "trialing"
    row = _row("user_lookalike")
    assert row is not None
    assert row.subscription_status == "trialing"
    assert row.trial_ends_at is not None


# ── Billing gate (database.get_or_create_customer → is_subscription_active) ──

@pytest.mark.parametrize("email", REAL_DEMO_VARIANTS)
def test_get_or_create_real_demo_gets_no_trial(email):
    _reset()
    from product.database import get_or_create_customer
    with patch("product.auth_clerk.fetch_clerk_user_profile"):
        get_or_create_customer("user_demo_goc", email, "Sarah")
    row = _row("user_demo_goc")
    assert row.subscription_status == "none"
    assert row.trial_ends_at is None


@pytest.mark.parametrize("email", LOOKALIKES)
def test_get_or_create_lookalike_is_treated_as_normal_customer(email):
    _reset()
    from product.database import get_or_create_customer
    with patch("product.auth_clerk.fetch_clerk_user_profile"):
        get_or_create_customer("user_lookalike_goc", email, "Look Alike")
    row = _row("user_lookalike_goc")
    assert row.subscription_status == "trialing"
    assert row.trial_ends_at is not None
    # The gate evaluates a lookalike on its own trial, not the demo bypass.
    assert is_subscription_active(row.subscription_status, row.trial_ends_at) is True


def test_sarah_c_csm_id_still_bypasses_provisioning():
    """The real demo identity keeps working exactly as before."""
    _reset()
    from product.subscription import provision_customer_on_first_auth
    assert provision_customer_on_first_auth("sarah_c", None) is None
    assert _row("sarah_c") is None


# ── CSM API key registry (main.py) ───────────────────────────────────────────

import main  # noqa: E402


def _req(headers: dict) -> MagicMock:
    r = MagicMock()
    r.headers = headers
    return r


def test_registry_has_no_default_key_when_env_unset(monkeypatch):
    monkeypatch.delenv("CSM_API_KEY_SARAH_C", raising=False)
    assert main._build_csm_registry() == {}
    monkeypatch.setenv("CSM_API_KEY_SARAH_C", "   ")
    assert main._build_csm_registry() == {}


def test_missing_api_key_is_rejected_not_defaulted(monkeypatch):
    monkeypatch.delenv("CSM_API_KEY_SARAH_C", raising=False)
    monkeypatch.setattr(main, "CSM_REGISTRY", main._build_csm_registry())
    for headers in ({}, {"X-CSM-API-Key": ""}, {"X-CSM-API-Key": "dev-sarah-c-key"}):
        assert main.resolve_user_id(_req(headers)) is None
        with pytest.raises(HTTPException) as exc:
            main.require_user_id(_req(headers))
        assert exc.value.status_code == 401


def test_configured_api_key_still_resolves(monkeypatch):
    monkeypatch.setenv("CSM_API_KEY_SARAH_C", "configured-key")
    monkeypatch.setattr(main, "CSM_REGISTRY", main._build_csm_registry())
    assert main.require_user_id(_req({"X-CSM-API-Key": "configured-key"})) == "sarah_c"
    with pytest.raises(HTTPException):
        main.require_user_id(_req({"X-CSM-API-Key": "dev-sarah-c-key"}))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
