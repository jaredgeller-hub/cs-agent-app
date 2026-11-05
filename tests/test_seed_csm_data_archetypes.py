"""
tests/test_seed_csm_data_archetypes.py

Smoke tests for scripts/seed_csm_data.py:
  - _archetype_for_account is deterministic across calls
  - _archetype_for_account distributes (not all accounts get the same one)
  - _render_thread runs without KeyError on a sample HubSpot contact
  - secondary archetype always differs from primary
  - hash-based archetype selection is consistent for same csm_id
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from random import Random
from zoneinfo import ZoneInfo

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _import_seed_module():
    """Import scripts/seed_csm_data.py via spec_from_file_location.

    The script lives outside the importable package tree, so this is the
    minimum-fuss way to access its module symbols from a pytest session.
    Registering the module in sys.modules is required so dataclasses
    introspection can find the parent module by __module__ lookup.
    """
    if "seed_csm_data" in sys.modules:
        return sys.modules["seed_csm_data"]
    spec = importlib.util.spec_from_file_location(
        "seed_csm_data",
        str(REPO_ROOT / "scripts" / "seed_csm_data.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seed_csm_data"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def seed_mod():
    return _import_seed_module()


# ── Determinism ──────────────────────────────────────────────────────────────


def test_archetype_assignment_is_deterministic(seed_mod):
    """Same (csm_id, account_name) produces the same archetype across calls."""
    a1 = seed_mod._archetype_for_account("user_abc123", "Meridian Health")
    a2 = seed_mod._archetype_for_account("user_abc123", "Meridian Health")
    a3 = seed_mod._archetype_for_account("user_abc123", "Meridian Health")
    assert a1 == a2 == a3


def test_archetype_assignment_varies_across_accounts(seed_mod):
    """Same csm_id + different accounts produces at least 2 distinct archetypes
    over a 15-account sample. (Hash distribution; almost-certain unless broken.)"""
    csm_id = "user_test_distribution"
    accounts = [
        "Meridian Health", "Beacon Financial", "Sutton Retail",
        "Northgate Logistics", "Pinnacle Group", "Central Banking Solutions",
        "Pulse Healthcare", "Horizon Education", "Atlas Medical Systems",
        "Crestview Technologies", "Silverline Finance", "Coastal Health Network",
        "Nova Aerospace", "Vantage Energy", "Summit Consulting",
    ]
    seen = {seed_mod._archetype_for_account(csm_id, n) for n in accounts}
    assert len(seen) >= 2


def test_archetype_assignment_differs_across_csms(seed_mod):
    """Different csm_id values may produce different archetype assignments."""
    a1 = seed_mod._archetype_for_account("user_alpha", "Meridian Health")
    a2 = seed_mod._archetype_for_account("user_beta", "Meridian Health")
    # We can't assert they always differ (hash collisions are possible), but
    # the values must each be valid archetype keys.
    assert a1 in seed_mod.ARCHETYPE_KEYS
    assert a2 in seed_mod.ARCHETYPE_KEYS


def test_secondary_archetype_differs_from_primary(seed_mod):
    """Secondary archetype is always a different archetype than the primary."""
    for primary in seed_mod.ARCHETYPE_KEYS:
        secondary = seed_mod._secondary_archetype(primary)
        assert secondary != primary
        assert secondary in seed_mod.ARCHETYPE_KEYS


# ── Render ───────────────────────────────────────────────────────────────────


def test_render_thread_no_keyerror_on_sample_contact(seed_mod):
    """_render_thread renders every archetype against a sample HubSpot
    contact dict without raising KeyError on the format() calls."""
    sample_account = {"name": "Meridian Health", "domain": "meridian.example"}
    sample_contact = {
        "name": "Marcus Reyes",
        "email": "marcus.reyes@meridian.example",
        "role": "VP Operations",
    }
    rng = Random(42)
    tz = ZoneInfo("America/Chicago")

    for archetype_key in seed_mod.ARCHETYPE_KEYS:
        thread = seed_mod._render_thread(
            archetype_key=archetype_key,
            account=sample_account,
            contact=sample_contact,
            csm_first_name="Sarah",
            csm_email="sarah.c@clarify.example",
            rng=rng,
            window_days=90,
            tz=tz,
        )
        assert isinstance(thread, list)
        assert len(thread) >= 2
        for msg in thread:
            assert "from" in msg and msg["from"]
            assert "to" in msg and msg["to"]
            assert "subject" in msg and msg["subject"].startswith(seed_mod.SEED_PREFIX)
            assert "body" in msg and msg["body"]
            assert isinstance(msg["date"], datetime)
            # Subject must reference the account name
            assert "Meridian Health" in msg["subject"]


def test_render_thread_handles_empty_contact_first_name(seed_mod):
    """A contact whose name parses to empty string still renders without error."""
    sample_account = {"name": "Beacon Financial", "domain": "beacon.example"}
    contact_no_name = {"name": "", "email": "ops@beacon.example", "role": ""}
    rng = Random(0)
    tz = ZoneInfo("UTC")

    # Use an arbitrary archetype
    thread = seed_mod._render_thread(
        archetype_key=seed_mod.ARCHETYPE_KEYS[0],
        account=sample_account,
        contact=contact_no_name,
        csm_first_name="Sarah",
        csm_email="sarah@example.com",
        rng=rng,
        window_days=90,
        tz=tz,
    )
    assert len(thread) >= 1


# ── Validation helpers ──────────────────────────────────────────────────────



def _operator_addresses() -> tuple:
    """Addresses on the operator's own domains, taken from settings (demo
    account, support and sender addresses); the seeder must block them."""
    import product.settings as _s
    return tuple(
        f"{local}@{addr.rsplit('@', 1)[1]}"
        for local, addr in (("frank", _s.DEMO_EMAIL), ("grace", _s.SUPPORT_EMAIL), ("heidi", _s.FROM_EMAIL))
    )


def test_validate_attendee_email_blocks_real_domains(seed_mod):
    """Real consumer-domain emails must be rejected to prevent invite leakage."""
    for blocked in (
        "alice@gmail.com", "bob@outlook.com", "carol@yahoo.com",
        "dave@hotmail.com", "eve@icloud.com",
        *_operator_addresses(),
    ):
        ok, reason = seed_mod._validate_attendee_email(blocked)
        assert not ok, f"expected reject for {blocked}: got ok=True"
        assert "blocked" in reason.lower() or "domain" in reason.lower()


def test_validate_attendee_email_allows_synthetic_domains(seed_mod):
    """Synthetic per-company domains (HubSpot sandbox shape) must pass."""
    for allowed in (
        "marcus@meridianhealth.example",
        "priya@beaconfinancial.example",
        "dana@northgate.example",
    ):
        ok, _reason = seed_mod._validate_attendee_email(allowed)
        assert ok, f"expected accept for {allowed}: got ok=False"


def test_validate_attendee_email_rejects_unparseable(seed_mod):
    """No '@' in the email → not parseable → reject."""
    for bad in ("", "not-an-email", "user-only"):
        ok, _ = seed_mod._validate_attendee_email(bad)
        assert not ok
