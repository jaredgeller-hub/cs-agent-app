"""
tests/test_disconnect_crm.py

disconnect_crm is the per-CRM disconnect primitive: ONE transaction that
deletes a single CRM's connections row and, per the "crm-match" rule, the
user_context_snapshots row when (and only when) that snapshot was built from
the CRM being disconnected. Far narrower than purge_user_data — it touches
exactly two tables and never the customers root row.

The crm-match rule (locked):
  - delete the snapshot IFF its stored crm_type == provider;
  - if crm_type is undeterminable (no row, context_json empty/"{}", or
    crm_type absent/unparseable), delete the snapshot only when NO CRM token
    remains after this disconnect (the OTHER CRM has no connections row);
  - if crm_type is the OTHER CRM, keep the snapshot.

Core assertions:
  - single-CRM (crm_type == provider): both rows deleted.
  - dual-CRM, disconnect NON-source CRM: token deleted, snapshot KEPT.
  - dual-CRM, disconnect SOURCE CRM: both deleted.
  - undeterminable crm_type + no other CRM: snapshot deleted.
  - undeterminable crm_type + other CRM present: snapshot KEPT.
  - no snapshot row: token_deleted True, snapshot_deleted False, no error.
  - invalid provider (incl. "google"): ValueError.
  - atomic rollback: force the snapshot delete to fail; the token row must
    still be present afterward (no partial delete).

Usage:
    python3 -m pytest tests/test_disconnect_crm.py -v
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import (
    Base,
    Connection,
    UserContextSnapshot,
    disconnect_crm,
)


# ---------------------------------------------------------------------------
# Test database setup (mirrors tests/test_purge_user_data.py)
# ---------------------------------------------------------------------------

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
    """Rebind SessionLocal before every test (CLAUDE.md invariant).

    disconnect_crm reads `SessionLocal` from the module at call time, so
    rebinding the module attribute routes its session to the in-memory engine.
    Each test starts from a clean schema.
    """
    _db.SessionLocal = _TestSession
    Base.metadata.drop_all(_test_engine)
    Base.metadata.create_all(_test_engine)
    yield
    _db.SessionLocal = _TestSession


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def seed_connection(session, uid: str, provider: str) -> None:
    session.add(Connection(
        id=str(uuid.uuid4()),
        customer_id=uid,
        provider=provider,
        token_data={"token": f"{provider}-x"},
        status="active",
    ))
    session.commit()


def seed_snapshot(session, uid: str, crm_type) -> None:
    """Seed a snapshot row. crm_type=None seeds an undeterminable "{}" body;
    a string seeds a body with that crm_type."""
    if crm_type is None:
        body = "{}"
    else:
        body = json.dumps({"crm_type": crm_type, "accounts": []})
    session.add(UserContextSnapshot(
        csm_id=uid,
        context_json=body,
        refreshed_at=datetime.now(timezone.utc),
        refresh_status="complete",
    ))
    session.commit()


def conn_count(session, uid: str, provider: str) -> int:
    return session.query(Connection).filter_by(
        customer_id=uid, provider=provider
    ).count()


def snap_count(session, uid: str) -> int:
    return session.query(UserContextSnapshot).filter_by(csm_id=uid).count()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_single_crm_crm_match_deletes_both_rows():
    uid = "user_single"
    s = _TestSession()
    seed_connection(s, uid, "hubspot")
    seed_snapshot(s, uid, "hubspot")
    s.close()

    result = disconnect_crm(uid, "hubspot")

    assert result == {
        "token_deleted": True,
        "snapshot_deleted": True,
        "snapshot_crm_type": "hubspot",
    }
    s = _TestSession()
    assert conn_count(s, uid, "hubspot") == 0
    assert snap_count(s, uid) == 0
    s.close()


def test_dual_crm_disconnect_non_source_keeps_snapshot():
    """Snapshot built from Gainsight; disconnect HubSpot → token gone, snapshot kept."""
    uid = "user_dual_nonsource"
    s = _TestSession()
    seed_connection(s, uid, "hubspot")
    seed_connection(s, uid, "gainsight")
    seed_snapshot(s, uid, "gainsight")
    s.close()

    result = disconnect_crm(uid, "hubspot")

    assert result == {
        "token_deleted": True,
        "snapshot_deleted": False,
        "snapshot_crm_type": "gainsight",
    }
    s = _TestSession()
    assert conn_count(s, uid, "hubspot") == 0
    assert conn_count(s, uid, "gainsight") == 1   # other CRM token untouched
    assert snap_count(s, uid) == 1                # snapshot kept
    s.close()


def test_dual_crm_disconnect_source_deletes_both():
    """Snapshot built from HubSpot; disconnect HubSpot → both deleted, Gainsight kept."""
    uid = "user_dual_source"
    s = _TestSession()
    seed_connection(s, uid, "hubspot")
    seed_connection(s, uid, "gainsight")
    seed_snapshot(s, uid, "hubspot")
    s.close()

    result = disconnect_crm(uid, "hubspot")

    assert result == {
        "token_deleted": True,
        "snapshot_deleted": True,
        "snapshot_crm_type": "hubspot",
    }
    s = _TestSession()
    assert conn_count(s, uid, "hubspot") == 0
    assert conn_count(s, uid, "gainsight") == 1
    assert snap_count(s, uid) == 0
    s.close()


def test_undeterminable_crm_type_no_other_crm_deletes_snapshot():
    uid = "user_undet_solo"
    s = _TestSession()
    seed_connection(s, uid, "hubspot")
    seed_snapshot(s, uid, None)   # empty "{}" body → undeterminable
    s.close()

    result = disconnect_crm(uid, "hubspot")

    assert result == {
        "token_deleted": True,
        "snapshot_deleted": True,
        "snapshot_crm_type": None,
    }
    s = _TestSession()
    assert conn_count(s, uid, "hubspot") == 0
    assert snap_count(s, uid) == 0
    s.close()


def test_undeterminable_crm_type_other_crm_present_keeps_snapshot():
    uid = "user_undet_dual"
    s = _TestSession()
    seed_connection(s, uid, "hubspot")
    seed_connection(s, uid, "gainsight")
    seed_snapshot(s, uid, None)
    s.close()

    result = disconnect_crm(uid, "hubspot")

    assert result == {
        "token_deleted": True,
        "snapshot_deleted": False,
        "snapshot_crm_type": None,
    }
    s = _TestSession()
    assert conn_count(s, uid, "hubspot") == 0
    assert conn_count(s, uid, "gainsight") == 1
    assert snap_count(s, uid) == 1   # kept: a CRM token remains
    s.close()


def test_no_snapshot_row_token_deleted_no_error():
    uid = "user_no_snapshot"
    s = _TestSession()
    seed_connection(s, uid, "hubspot")
    s.close()

    result = disconnect_crm(uid, "hubspot")

    assert result == {
        "token_deleted": True,
        "snapshot_deleted": False,
        "snapshot_crm_type": None,
    }
    s = _TestSession()
    assert conn_count(s, uid, "hubspot") == 0
    assert snap_count(s, uid) == 0
    s.close()


def test_no_token_row_no_snapshot_is_a_noop_success():
    """Nothing to delete → both False, no error (idempotent re-disconnect)."""
    uid = "user_nothing"
    result = disconnect_crm(uid, "gainsight")
    assert result == {
        "token_deleted": False,
        "snapshot_deleted": False,
        "snapshot_crm_type": None,
    }


@pytest.mark.parametrize("bad", ["google", "gmail", "calendar", "salesforce", ""])
def test_invalid_provider_raises_value_error(bad):
    with pytest.raises(ValueError):
        disconnect_crm("user_x", bad)


def test_atomic_rollback_when_snapshot_delete_fails():
    """Force the snapshot delete to raise AFTER the token delete has executed
    in-session; the rollback must restore the token row — no partial delete."""
    uid = "user_rollback"
    s = _TestSession()
    seed_connection(s, uid, "hubspot")
    seed_snapshot(s, uid, "hubspot")   # crm_match → would delete both
    s.close()

    # A session whose .delete() raises on the snapshot only. The connection
    # delete (step c) runs first and succeeds in-session; the snapshot delete
    # (step d) then raises, forcing the except→rollback path.
    prepared = _TestSession()
    orig_delete = prepared.delete

    def bad_delete(obj, *a, **k):
        if isinstance(obj, UserContextSnapshot):
            raise RuntimeError("forced snapshot delete failure")
        return orig_delete(obj, *a, **k)

    prepared.delete = bad_delete
    _db.SessionLocal = lambda: prepared
    try:
        result = disconnect_crm(uid, "hubspot")
    finally:
        _db.SessionLocal = _TestSession

    # Never raises out; both flags False because the transaction rolled back.
    assert result == {
        "token_deleted": False,
        "snapshot_deleted": False,
        "snapshot_crm_type": None,
    }
    # Rollback restored the connection row, and the snapshot is still present.
    s = _TestSession()
    assert conn_count(s, uid, "hubspot") == 1, "rollback must restore the token row"
    assert snap_count(s, uid) == 1
    s.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
