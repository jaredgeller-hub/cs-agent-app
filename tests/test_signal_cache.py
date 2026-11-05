"""
tests/test_signal_cache.py

Regression guards for product/signal_cache.py write/read semantics.

Invariants under test (A.1.5 Commit 2):
  1. Round-trip: payloads written are returned verbatim on read.
  2. Upsert on duplicate (csm_id, signal_type, signal_id).
  3. Read's signal_type filter (None returns all types).
  4. Read's freshness window excludes stale rows.
  5. Empty-list write is a true no-op (zero rows touched).
  6. Read returns [] not None for the never-written case.

Isolation: in-memory SQLite engine, product.database.SessionLocal
monkey-patched to a Session factory pointing at the test engine.
No prod DB touched, no network.

Standalone-runnable:
    python3 tests/test_signal_cache.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import pytest

import product.database as _db
from product.database import Base, SignalCache


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────

_test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _bind_session_to_this_files_engine():
    """Other test files (test_oauth_state, test_expire_trials) also
    monkey-patch _db.SessionLocal at module import; whichever imports
    later wins. Rebind before every test in this file.
    """
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_table():
    """Truncate signal_cache between tests."""
    session = _TestSession()
    try:
        session.query(SignalCache).delete()
        session.commit()
    finally:
        session.close()
    yield


# Import AFTER patching so signal_cache sees the test SessionLocal.
from product.signal_cache import write_signals, read_signals


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_signal(signal_type: str, thread_id: str, contact: str, account: str) -> dict:
    """Build a full 8-key gmail signal dict matching Commit 1's shape."""
    return {
        "signal_type": signal_type,
        "account": account,
        "contact": contact,
        "detail": f"Test detail for {thread_id}",
        "days_waiting": 5,
        "thread_subject": f"Subject for {thread_id}",
        "timestamp": "2026-04-20T10:00:00+00:00",
        "thread_id": thread_id,
    }


# ── Tests ────────────────────────────────────────────────────────────────────

def test_write_then_read_roundtrip():
    """Write 2 signals, read back, assert full payloads preserved verbatim."""
    sigs = [
        _make_signal("email.absence", "thr-1", "a@x.example", "AcctA"),
        _make_signal("email.absence", "thr-2", "b@x.example", "AcctA"),
    ]
    n = write_signals("user_rt", "email.absence", sigs)
    assert n == 2

    payloads = read_signals("user_rt", signal_type="email.absence")
    assert len(payloads) == 2

    # Payloads are the full 8-key dicts, identical to inputs.
    by_thread = {p["thread_id"]: p for p in payloads}
    assert by_thread["thr-1"] == sigs[0]
    assert by_thread["thr-2"] == sigs[1]
    print("✓ roundtrip: 2 signals written, 2 payloads read verbatim")


def test_write_upserts_on_duplicate_signal_id():
    """Same (thread_id, contact, account) → same signal_id → upsert, not duplicate."""
    sig_v1 = _make_signal("email.received", "thr-dup", "c@x.example", "AcctB")
    sig_v2 = dict(sig_v1, detail="UPDATED detail")

    assert write_signals("user_dup", "email.received", [sig_v1]) == 1
    assert write_signals("user_dup", "email.received", [sig_v2]) == 1

    session = _TestSession()
    try:
        rows = session.query(SignalCache).filter_by(csm_id="user_dup").all()
        assert len(rows) == 1, f"expected 1 row after upsert, got {len(rows)}"
        assert rows[0].signal_payload["detail"] == "UPDATED detail"
    finally:
        session.close()
    print("✓ upsert: duplicate signal_id produces single row with updated payload")


def test_read_signal_type_filter():
    """Filter on signal_type; None returns all types."""
    write_signals("user_flt", "email.absence",
                  [_make_signal("email.absence", "thr-a", "a@x.example", "AcctC")])
    write_signals("user_flt", "email.received",
                  [_make_signal("email.received", "thr-r", "r@x.example", "AcctC")])

    absences = read_signals("user_flt", signal_type="email.absence")
    assert len(absences) == 1
    assert absences[0]["signal_type"] == "email.absence"

    receiveds = read_signals("user_flt", signal_type="email.received")
    assert len(receiveds) == 1
    assert receiveds[0]["signal_type"] == "email.received"

    all_types = read_signals("user_flt", signal_type=None)
    assert len(all_types) == 2
    assert {s["signal_type"] for s in all_types} == {"email.absence", "email.received"}
    print("✓ filter: type-scoped reads return only matching rows; None returns all")


def test_read_excludes_stale_rows():
    """Rows older than max_age_minutes are excluded from reads."""
    write_signals("user_stale", "email.absence",
                  [_make_signal("email.absence", "thr-old", "o@x.example", "AcctD")])

    # Manually age the row beyond the default 60-minute window.
    session = _TestSession()
    try:
        row = session.query(SignalCache).filter_by(csm_id="user_stale").first()
        row.refreshed_at = datetime.now(timezone.utc) - timedelta(minutes=120)
        session.commit()
    finally:
        session.close()

    assert read_signals("user_stale", max_age_minutes=60) == []
    # Widen the window; stale row reappears.
    assert len(read_signals("user_stale", max_age_minutes=180)) == 1
    print("✓ freshness: rows aged past max_age_minutes excluded; widening the window re-includes them")


def test_write_empty_list_is_noop():
    """Empty signal list → returns 0, no rows written, no DB round-trip (early return)."""
    n = write_signals("user_noop", "email.absence", [])
    assert n == 0

    session = _TestSession()
    try:
        assert session.query(SignalCache).filter_by(csm_id="user_noop").count() == 0
    finally:
        session.close()
    print("✓ noop: empty list write returns 0 and writes zero rows")


def test_read_returns_empty_list_not_none():
    """Never-written csm_id → returns [], never None."""
    result = read_signals("user_never_written")
    assert result == []
    assert result is not None
    assert isinstance(result, list)
    print("✓ empty read: returns [] (empty list), not None")


def main():
    """Standalone runner (same pattern as other tests/test_*.py files)."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
