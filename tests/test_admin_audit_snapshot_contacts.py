"""
tests/test_admin_audit_snapshot_contacts.py

Regression coverage for the contact_names_per_account query added to
/admin/audit-snapshot. Verifies:

  1. Missing snapshot for csm_id -> snapshot_status="missing", empty
     accounts and no marcus_grep matches.
  2. Snapshot containing two contacts under "Meridian Health" -> account
     row populated with contact_count=2 and both names.
  3. Snapshot with "Marcus Reyes" anywhere in raw context_json ->
     marcus_grep includes a "marcus" pattern hit.
  4. Lowercase "marcus" in raw context_json -> still matched
     (case-insensitive grep).
  5. Account with zero cached contacts -> still appears in accounts list
     with contact_count=0.
  6. ?csm_id=... selects the snapshot; it is required (400 without it).

Fixture pattern follows tests/test_actions_execute_email_draft.py:
in-memory SQLite + StaticPool + monkey-patched product.database.SessionLocal.
ADMIN_SECRET is forced into the environment before main is imported so the
endpoint accepts our test header.

Standalone-runnable:
    python3 tests/test_admin_audit_snapshot_contacts.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, UserContextSnapshot

import main

# Force a known ADMIN_SECRET AFTER importing main, so any dotenv load that
# happens at import time does not overwrite it. The handler reads
# os.getenv("ADMIN_SECRET") at request time, so this takes effect for every
# request issued by the TestClient.
_ADMIN_KEY = "test_admin_secret_audit_snapshot"
os.environ["ADMIN_SECRET"] = _ADMIN_KEY


# in-memory SQLite shared across handler threads
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
    """Rebind SessionLocal before every test, per CLAUDE.md invariant for
    test files that monkey-patch _db.SessionLocal."""
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _force_admin_secret():
    """Re-set ADMIN_SECRET before every test. Sibling test modules that
    also set this env var at import time can race during collection;
    binding it per-test makes our test header reliable."""
    os.environ["ADMIN_SECRET"] = _ADMIN_KEY
    yield


@pytest.fixture(autouse=True)
def _reset_snapshots():
    """Truncate user_context_snapshots between tests."""
    s = _TestSession()
    try:
        s.query(UserContextSnapshot).delete()
        s.commit()
    finally:
        s.close()
    yield


client = TestClient(main.app)

# csm_id is a required query param; this neutral id is passed explicitly.
DEFAULT_CSM = "user_audit_contacts_default_csm"


def _seed_snapshot(csm_id: str, payload: dict, refreshed_at=None, status="complete"):
    """Insert a UserContextSnapshot row directly."""
    s = _TestSession()
    try:
        row = UserContextSnapshot(
            csm_id=csm_id,
            context_json=json.dumps(payload),
            refreshed_at=refreshed_at or datetime.now(timezone.utc),
            refresh_status=status,
        )
        s.add(row)
        s.commit()
    finally:
        s.close()


def _post_audit(csm_id: str = DEFAULT_CSM):
    qs = f"?csm_id={csm_id}"
    r = client.post(
        f"/admin/audit-snapshot{qs}",
        headers={"X-Admin-Key": _ADMIN_KEY},
    )
    assert r.status_code == 200, f"audit endpoint returned {r.status_code}: {r.text[:300]}"
    body = r.json()
    return body["queries"]["contact_names_per_account"]


# ── Tests ────────────────────────────────────────────────────────────────────

def test_missing_snapshot_returns_status_missing():
    block = _post_audit()
    assert block is not None, "contact_names_per_account block should exist even with no snapshot"
    assert block["csm_id"] == DEFAULT_CSM
    assert block["snapshot_status"] == "missing"
    assert block["snapshot_age_minutes"] is None
    assert block["accounts"] == []
    assert block["marcus_grep"]["matches"] == []
    assert block["marcus_grep"]["patterns_searched"] == ["marcus", "marc", "reyes"]
    assert block["cache_caveats"]["per_account_contact_cap"] == 3
    assert block["cache_caveats"]["total_contact_cap"] == 60
    print("OK missing snapshot -> snapshot_status=missing, empty accounts and matches")


def test_two_contacts_for_meridian_health():
    payload = {
        "accounts": [{"name": "Meridian Health", "hubspot_id": "111"}],
        "contacts": [
            {"name": "Alice Wong", "email": "alice@meridian.example", "account_name": "Meridian Health"},
            {"name": "Bob Liu", "email": "bob@meridian.example", "account_name": "Meridian Health"},
        ],
    }
    _seed_snapshot(DEFAULT_CSM, payload)

    block = _post_audit()
    assert block["snapshot_status"] == "complete"
    assert len(block["accounts"]) == 1
    acct = block["accounts"][0]
    assert acct["account_name"] == "Meridian Health"
    assert acct["contact_count"] == 2
    assert sorted(acct["contact_names"]) == ["Alice Wong", "Bob Liu"]
    print("OK 2 contacts for Meridian Health -> contact_count=2, both names returned")


def test_marcus_reyes_match_in_grep():
    payload = {
        "accounts": [{"name": "Meridian Health"}],
        "contacts": [
            {"name": "Marcus Reyes", "email": "mreyes@meridian.example",
             "account_name": "Meridian Health"},
        ],
    }
    _seed_snapshot(DEFAULT_CSM, payload)

    block = _post_audit()
    matches = block["marcus_grep"]["matches"]
    assert any(m["pattern"] == "marcus" for m in matches), \
        f"expected at least one 'marcus' pattern hit, got {matches!r}"
    # "Marcus Reyes" should also surface a "reyes" match
    assert any(m["pattern"] == "reyes" for m in matches), \
        f"expected at least one 'reyes' pattern hit, got {matches!r}"
    # Each match context should include the matched substring
    for m in matches:
        assert m["pattern"].lower() in m["context"].lower()
    print("OK Marcus Reyes in payload -> marcus_grep matches both 'marcus' and 'reyes'")


def test_grep_is_case_insensitive_lowercase_marcus():
    payload = {
        "accounts": [{"name": "Acme Co"}],
        "contacts": [
            {"name": "marcus chen", "email": "marcus@acme.example",
             "account_name": "Acme Co"},
        ],
    }
    _seed_snapshot(DEFAULT_CSM, payload)

    block = _post_audit()
    matches = block["marcus_grep"]["matches"]
    assert any(m["pattern"] == "marcus" for m in matches), \
        f"lowercase 'marcus' should match case-insensitive grep; got {matches!r}"
    print("OK lowercase 'marcus' -> matched case-insensitively")


def test_account_with_zero_contacts_still_appears():
    payload = {
        "accounts": [
            {"name": "Beacon Financial"},
            {"name": "Pinnacle Group"},
        ],
        "contacts": [
            {"name": "Priya Sharma", "email": "priya@beacon.example",
             "account_name": "Beacon Financial"},
        ],
    }
    _seed_snapshot(DEFAULT_CSM, payload)

    block = _post_audit()
    by_name = {a["account_name"]: a for a in block["accounts"]}
    assert "Beacon Financial" in by_name
    assert by_name["Beacon Financial"]["contact_count"] == 1

    assert "Pinnacle Group" in by_name, \
        "account with zero cached contacts should still appear in the response"
    assert by_name["Pinnacle Group"]["contact_count"] == 0
    assert by_name["Pinnacle Group"]["contact_names"] == []
    print("OK account with 0 contacts still listed with contact_count=0")


def test_csm_id_query_param_override():
    other_csm = "user_other_csm_id_for_override_test"
    payload_default = {
        "accounts": [{"name": "Default Account"}],
        "contacts": [],
    }
    payload_other = {
        "accounts": [{"name": "Override Account"}],
        "contacts": [
            {"name": "Override Contact", "email": "x@y.example",
             "account_name": "Override Account"},
        ],
    }
    _seed_snapshot(DEFAULT_CSM, payload_default)
    _seed_snapshot(other_csm, payload_other)

    # First csm -> its own snapshot only
    default_block = _post_audit(csm_id=DEFAULT_CSM)
    assert default_block["csm_id"] == DEFAULT_CSM
    assert any(a["account_name"] == "Default Account" for a in default_block["accounts"])

    # Second csm -> its own snapshot only
    override_block = _post_audit(csm_id=other_csm)
    assert override_block["csm_id"] == other_csm
    names = {a["account_name"] for a in override_block["accounts"]}
    assert "Override Account" in names
    assert "Default Account" not in names, \
        "override should not bleed in default csm's accounts"
    print("OK ?csm_id= selects the snapshot")


def test_missing_csm_id_returns_400():
    """No default identity: the endpoint refuses without ?csm_id=."""
    for qs in ("", "?csm_id=", "?csm_id=%20%20"):
        r = client.post(f"/admin/audit-snapshot{qs}", headers={"X-Admin-Key": _ADMIN_KEY})
        assert r.status_code == 400, f"{qs!r} -> {r.status_code}"
        assert "csm_id" in r.json()["detail"]


def main_runner():
    tests = [
        test_missing_snapshot_returns_status_missing,
        test_two_contacts_for_meridian_health,
        test_marcus_reyes_match_in_grep,
        test_grep_is_case_insensitive_lowercase_marcus,
        test_account_with_zero_contacts_still_appears,
        test_csm_id_query_param_override,
        test_missing_csm_id_returns_400,
    ]
    failures = 0
    for t in tests:
        # Reset snapshots between standalone runs (no autouse fixture path here)
        s = _TestSession()
        try:
            s.query(UserContextSnapshot).delete()
            s.commit()
        finally:
            s.close()
        _db.SessionLocal = _TestSession
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    if failures:
        print(f"{failures} test(s) failed")
        sys.exit(1)
    print("all tests passed")


if __name__ == "__main__":
    main_runner()
