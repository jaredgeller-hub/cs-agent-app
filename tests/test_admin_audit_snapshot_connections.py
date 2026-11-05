"""
tests/test_admin_audit_snapshot_connections.py

Regression coverage for the connection_state_per_user query exposed by
/admin/audit-snapshot.

After Stage 2a token encryption (May 2026), the audit endpoint no longer
reads tokens/{csm_id}.json. The filesystem_token sub-block is preserved
in the response shape for backward compatibility but always reports
"missing" (path_checked=[], found_at=None, scopes_in_file=None, both
scope flags False). The pilot_health_check_result.calendar_api signal
degrades to a constant "no_token" because it relied on the FS scope
read. The users_with_scope_mismatch summary counter degrades to 0.
The architectural finding pilot_health_check_uses_filesystem_for_calendar_scope
flips to False.

These tests pin the new degraded behavior so a future refactor that
restores or further removes the column has to make a deliberate choice.

Fixture pattern follows tests/test_admin_audit_snapshot_contacts.py:
in-memory SQLite + StaticPool + monkey-patched product.database.SessionLocal.
ADMIN_SECRET is forced AFTER importing main so dotenv does not clobber it.

Standalone-runnable:
    python3 tests/test_admin_audit_snapshot_connections.py
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer, Connection

import main

_ADMIN_KEY = "test_admin_secret_audit_connections"
os.environ["ADMIN_SECRET"] = _ADMIN_KEY


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
def _force_admin_secret():
    """Re-set ADMIN_SECRET before every test. Sibling test modules that
    also set this env var at import time can race during collection;
    binding it per-test makes our test header reliable."""
    os.environ["ADMIN_SECRET"] = _ADMIN_KEY
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


client = TestClient(main.app)


def _seed_customer(*, clerk_user_id: str, email: str = None) -> str:
    """Insert a Customer row and return its primary id."""
    cust_id = str(uuid.uuid4())
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


def _seed_connection(
    *, customer_id: str, provider: str, status: str = "active",
    token_data: dict | None = None, scopes_granted: str | None = None,
    last_verified_at: datetime | None = None,
):
    s = _TestSession()
    try:
        s.add(Connection(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            provider=provider,
            token_data=token_data,
            scopes_granted=scopes_granted,
            status=status,
            last_verified_at=last_verified_at or datetime.now(timezone.utc),
        ))
        s.commit()
    finally:
        s.close()


_AUDIT_CSM = "user_audit_connections_csm"


def _with_csm(query_string: str) -> str:
    """csm_id is a required query param on /admin/audit-snapshot."""
    sep = "&" if query_string.startswith("?") else "?"
    return f"{query_string}{sep}csm_id={_AUDIT_CSM}"


def _post_audit(query_string: str = ""):
    r = client.post(
        f"/admin/audit-snapshot{_with_csm(query_string)}",
        headers={"X-Admin-Key": _ADMIN_KEY},
    )
    assert r.status_code == 200, f"audit returned {r.status_code}: {r.text[:300]}"
    return r.json()["queries"]["connection_state_per_user"]


def _post_audit_provider_counts(query_string: str = ""):
    r = client.post(
        f"/admin/audit-snapshot{_with_csm(query_string)}",
        headers={"X-Admin-Key": _ADMIN_KEY},
    )
    assert r.status_code == 200, f"audit returned {r.status_code}: {r.text[:300]}"
    return r.json()["queries"]["connection_provider_counts"]


def _find_user(block, csm_id):
    for u in block["users"]:
        if u["csm_id"] == csm_id:
            return u
    raise AssertionError(f"csm_id {csm_id!r} not in users: "
                         f"{[u['csm_id'] for u in block['users']]}")


def test_filesystem_token_block_always_reports_missing():
    """Post-Stage 2a: FS read is gone. The filesystem_token sub-block must
    surface the degraded constants (path_checked=[], found_at=None, both
    scope flags False) for every user, regardless of DB state."""
    csm_id = "user_fs_block_shape"
    _seed_customer(clerk_user_id=csm_id)
    _seed_connection(
        customer_id=csm_id,
        provider="gmail",
        status="active",
        token_data={"access_token": "fake", "refresh_token": "ref"},
    )

    block = _post_audit()
    user = _find_user(block, csm_id)
    fs = user["providers"]["gmail"]["filesystem_token"]
    assert fs["path_checked"] == []
    assert fs["found_at"] is None
    assert fs["scopes_in_file"] is None
    assert fs["has_calendar_scope"] is False
    assert fs["has_gmail_scope"] is False


def test_calendar_api_degraded_to_no_token():
    """Post-Stage 2a: calendar_api always reports 'no_token' because the
    FS-scope read it depended on is gone."""
    csm_id = "user_cal_api_const"
    _seed_customer(clerk_user_id=csm_id)
    _seed_connection(
        customer_id=csm_id,
        provider="gmail",
        token_data={"access_token": "fake", "refresh_token": "ref"},
    )

    block = _post_audit()
    user = _find_user(block, csm_id)
    assert user["pilot_health_check_result"]["calendar_api"] == "no_token"


def test_summary_users_with_calendar_no_token_counts_every_user():
    """Every user trips users_with_calendar_no_token now that calendar_api
    is a constant 'no_token'. users_with_scope_mismatch stays 0 (the FS
    mismatch signal is gone)."""
    _seed_customer(clerk_user_id="u_a")
    _seed_customer(clerk_user_id="u_b")
    _seed_connection(
        customer_id="u_a", provider="gmail",
        token_data={"access_token": "ok", "refresh_token": "r"},
    )
    _seed_connection(
        customer_id="u_b", provider="gmail",
        token_data={"access_token": "ok", "refresh_token": "r"},
    )

    block = _post_audit()
    summary = block["connection_state_summary"]
    assert summary["users_total"] == 2
    assert summary["users_with_calendar_no_token"] == 2
    assert summary["users_with_scope_mismatch"] == 0


def test_users_with_status_lying_still_detected():
    """status_lying counts gmail rows with status=active but token_data
    missing/invalid. This is DB-only and unaffected by Stage 2a."""
    _seed_customer(clerk_user_id="status_lying_user")
    _seed_connection(
        customer_id="status_lying_user", provider="gmail", status="active",
        token_data=None,
    )

    block = _post_audit()
    summary = block["connection_state_summary"]
    assert summary["users_with_status_lying"] == 1


def test_architectural_findings_data_driven():
    """connection_status_is_dead_infrastructure and connection_table_only_holds_gmail
    are still data-driven. pilot_health_check_uses_filesystem_for_calendar_scope
    is now a constant False (Stage 2a flipped this finding)."""
    _seed_customer(clerk_user_id="arch_user_1")
    _seed_connection(
        customer_id="arch_user_1", provider="gmail",
        token_data={"access_token": "ok"},
    )

    block_initial = _post_audit()
    arch_initial = block_initial["architectural_findings"]
    assert arch_initial["connection_status_is_dead_infrastructure"] is True
    assert arch_initial["connection_table_only_holds_gmail"] is True
    assert arch_initial["scopes_granted_never_read"] is True
    # Flipped to False in Stage 2a — the FS read was removed.
    assert arch_initial["pilot_health_check_uses_filesystem_for_calendar_scope"] is False

    _seed_customer(clerk_user_id="arch_user_2")
    _seed_connection(
        customer_id="arch_user_2", provider="hubspot", status="expired",
        token_data={"access_token": "stale"},
    )

    block_after = _post_audit()
    arch_after = block_after["architectural_findings"]
    assert arch_after["connection_status_is_dead_infrastructure"] is False, (
        "non-active row should flip the flag"
    )
    assert arch_after["connection_table_only_holds_gmail"] is False, (
        "non-gmail row should flip the flag"
    )


def test_include_live_gmail_flag_runs_live_check():
    """Live gmail check is independent of FS read. ?include_live_gmail=1
    still invokes _load_credentials and reports gmail_api=ok on success.
    calendar_api remains 'no_token' (degraded constant)."""
    csm_id = "live_check_user"
    _seed_customer(clerk_user_id=csm_id)
    _seed_connection(
        customer_id=csm_id, provider="gmail",
        token_data={"access_token": "ok", "refresh_token": "r"},
    )

    fake_creds = MagicMock()
    fake_creds.expired = False

    with patch(
        "product.digest.delivery._load_credentials",
        return_value=fake_creds,
    ) as mock_lc:
        block = _post_audit("?include_live_gmail=1")

    user = _find_user(block, csm_id)
    live = user["providers"]["gmail"]["live_gmail_check"]
    assert live["ran"] is True
    assert live["result"] == "ok"
    assert mock_lc.called, "live check should invoke _load_credentials"
    assert user["pilot_health_check_result"]["gmail_api"] == "ok"
    # calendar_api still degraded
    assert user["pilot_health_check_result"]["calendar_api"] == "no_token"


def test_default_no_flag_skips_live_check():
    csm_id = "no_live_user"
    _seed_customer(clerk_user_id=csm_id)
    _seed_connection(
        customer_id=csm_id, provider="gmail",
        token_data={"access_token": "ok", "refresh_token": "r"},
    )

    with patch("product.digest.delivery._load_credentials") as mock_lc:
        block = _post_audit()

    user = _find_user(block, csm_id)
    live = user["providers"]["gmail"]["live_gmail_check"]
    assert live["ran"] is False
    assert live["result"] is None
    assert not mock_lc.called, "no flag means no network call"
    assert user["pilot_health_check_result"]["gmail_api"] == "skipped_live_check_disabled"


def test_lookup_falls_back_to_customer_id_uuid():
    """Connection.customer_id holding the Customer.id UUID (legacy/future
    write path) should still resolve via the fallback lookup branch."""
    csm_id = "fallback_user_uuid_path"
    cust_id = _seed_customer(clerk_user_id=csm_id)
    # Note: customer_id is the UUID, NOT the clerk_user_id. The audit
    # endpoint should fall back to Customer.id when clerk_user_id misses.
    _seed_connection(
        customer_id=cust_id,
        provider="gmail",
        status="active",
        token_data={"access_token": "fallback", "refresh_token": "ref"},
    )

    block = _post_audit()
    user = _find_user(block, csm_id)
    gmail = user["providers"]["gmail"]
    assert gmail["status"] == "active", (
        f"fallback lookup should resolve the row, got {gmail['status']!r}"
    )
    assert gmail["token_data_present"] is True
    assert gmail["token_data_appears_valid"] is True


def test_lookup_no_match_reports_missing():
    """Connection.customer_id matching neither clerk_user_id nor Customer.id
    means the row is orphaned. The user's gmail block must show 'missing'."""
    csm_id = "no_match_user"
    _seed_customer(clerk_user_id=csm_id)
    _seed_connection(
        customer_id="totally_unrelated_string_xyz",
        provider="gmail",
        status="active",
        token_data={"access_token": "orphan", "refresh_token": "r"},
    )

    block = _post_audit()
    user = _find_user(block, csm_id)
    gmail = user["providers"]["gmail"]
    assert gmail["status"] == "missing", (
        f"orphan row keyed by unrelated string should not resolve; "
        f"got {gmail['status']!r}"
    )
    assert gmail["token_data_present"] is False
    assert gmail["token_data_appears_valid"] is False


def test_provider_counts_basic_breakdown():
    """Two gmail rows, one calendar row -> by_provider counts and
    distinct_providers contain both providers."""
    _seed_customer(clerk_user_id="prov_user_a")
    _seed_customer(clerk_user_id="prov_user_b")
    _seed_connection(
        customer_id="prov_user_a", provider="gmail",
        token_data={"access_token": "ok"},
    )
    _seed_connection(
        customer_id="prov_user_b", provider="gmail",
        token_data={"access_token": "ok"},
    )
    _seed_connection(
        customer_id="prov_user_a", provider="calendar",
        token_data={"access_token": "ok"},
    )

    block = _post_audit_provider_counts()
    counts_by_prov = {entry["provider"]: entry["count"] for entry in block["by_provider"]}
    assert counts_by_prov == {"gmail": 2, "calendar": 1}, counts_by_prov
    assert block["total_rows"] == 3
    assert sorted(block["distinct_providers"]) == ["calendar", "gmail"]
    for entry in block["by_provider"]:
        assert entry["earliest_created_at"] is not None
        assert entry["latest_created_at"] is not None


def test_provider_counts_all_clerk_keyed():
    """All rows keyed by clerk_user_id -> non_clerk and orphan diagnostic
    counts are zero; uuid count is zero too (no row matches a UUID)."""
    _seed_customer(clerk_user_id="clerk_only_a")
    _seed_customer(clerk_user_id="clerk_only_b")
    _seed_connection(
        customer_id="clerk_only_a", provider="gmail",
        token_data={"access_token": "ok"},
    )
    _seed_connection(
        customer_id="clerk_only_b", provider="gmail",
        token_data={"access_token": "ok"},
    )

    block = _post_audit_provider_counts()
    assert block["total_rows"] == 2
    assert block["rows_with_non_clerk_customer_id"] == 0
    assert block["rows_with_uuid_customer_id"] == 0
    assert block["rows_with_orphan_customer_id"] == 0


def test_provider_counts_mixed_keys():
    """Mixed: 2 rows by clerk_user_id, 1 by UUID, 1 orphan ->
    non_clerk = uuid + orphan = 2, uuid = 1, orphan = 1."""
    _seed_customer(clerk_user_id="mixed_clerk_1")
    _seed_customer(clerk_user_id="mixed_clerk_2")
    uuid_target = _seed_customer(clerk_user_id="mixed_uuid_user")

    # 2 rows keyed by clerk_user_id
    _seed_connection(
        customer_id="mixed_clerk_1", provider="gmail",
        token_data={"access_token": "ok"},
    )
    _seed_connection(
        customer_id="mixed_clerk_2", provider="gmail",
        token_data={"access_token": "ok"},
    )
    # 1 row keyed by Customer.id UUID
    _seed_connection(
        customer_id=uuid_target, provider="gmail",
        token_data={"access_token": "ok"},
    )
    # 1 orphan row keyed by neither
    _seed_connection(
        customer_id="totally_unrelated_zzz", provider="gmail",
        token_data={"access_token": "ok"},
    )

    block = _post_audit_provider_counts()
    assert block["total_rows"] == 4
    # rows whose customer_id is NOT a clerk_user_id: the UUID row + the orphan
    assert block["rows_with_non_clerk_customer_id"] == 2
    assert block["rows_with_uuid_customer_id"] == 1
    assert block["rows_with_orphan_customer_id"] == 1


def test_provider_counts_empty_table():
    """Empty connections table -> all counts 0, distinct_providers=[]."""
    # Customer rows present but no Connection rows
    _seed_customer(clerk_user_id="empty_table_user")

    block = _post_audit_provider_counts()
    assert block["by_provider"] == []
    assert block["total_rows"] == 0
    assert block["distinct_providers"] == []
    assert block["rows_with_non_clerk_customer_id"] == 0
    assert block["rows_with_uuid_customer_id"] == 0
    assert block["rows_with_orphan_customer_id"] == 0


def main_runner():
    tests = [
        test_filesystem_token_block_always_reports_missing,
        test_calendar_api_degraded_to_no_token,
        test_summary_users_with_calendar_no_token_counts_every_user,
        test_users_with_status_lying_still_detected,
        test_architectural_findings_data_driven,
        test_include_live_gmail_flag_runs_live_check,
        test_default_no_flag_skips_live_check,
        test_lookup_falls_back_to_customer_id_uuid,
        test_lookup_no_match_reports_missing,
        test_provider_counts_basic_breakdown,
        test_provider_counts_all_clerk_keyed,
        test_provider_counts_mixed_keys,
        test_provider_counts_empty_table,
    ]
    failures = 0
    for t in tests:
        s = _TestSession()
        try:
            s.query(Connection).delete()
            s.query(Customer).delete()
            s.commit()
        finally:
            s.close()
        _db.SessionLocal = _TestSession
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    if failures:
        print(f"{failures} test(s) failed")
        sys.exit(1)
    print("all tests passed")


if __name__ == "__main__":
    main_runner()
