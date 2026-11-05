"""
tests/test_oauth_state.py

Verifies product.oauth_state_store's state lifecycle: save → ok → expired,
and the new get_state_with_status API that differentiates "not_found" vs.
"expired" (driven by the Apr 18 Google OAuth failure diagnosis).

Also covers:
- Duration logging on consume (INFO-level record fires with elapsed_seconds)
- One-time consumption invariant (consume twice → second call is not_found)

Structured as standalone-runnable today; can be promoted to pytest in
Day 3's CI work.

Isolation: in-memory SQLite engine pointed at Base.metadata. No prod DB
touched. product.database.SessionLocal is monkey-patched into the test
engine's Session factory; product.database.OAuthState is imported as-is
(the schema is portable).

Usage:
    python3 tests/test_oauth_state.py
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import product.database as _db
from product.database import Base, OAuthState


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────

_test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
# Patch the SessionLocal that oauth_state_store imports lazily
_db.SessionLocal = _TestSession

# Under pytest, other test files (test_expire_trials.py) also monkey-patch
# _db.SessionLocal at module import; whichever imports later wins. Re-bind
# before every test in this file. Standalone invocation doesn't need this.
try:
    import pytest

    @pytest.fixture(autouse=True)
    def _bind_session_to_this_files_engine():
        _db.SessionLocal = _TestSession
        yield
except ImportError:
    pass


def _reset_table():
    """Truncate oauth_states between tests."""
    session = _TestSession()
    try:
        session.query(OAuthState).delete()
        session.commit()
    finally:
        session.close()


# Import AFTER patching so the module sees the test SessionLocal
from product.oauth_state_store import (
    save_state,
    get_state,
    get_state_with_status,
    consume_state,
    _TTL_MINUTES,
)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_ttl_is_thirty_minutes():
    assert _TTL_MINUTES == 30, f"TTL should be 30min (was {_TTL_MINUTES})"
    print(f"✓ TTL is {_TTL_MINUTES} minutes")


def test_save_and_consume_within_ttl():
    _reset_table()
    assert save_state("state_abc", "google", "user_xyz") is True
    entry = consume_state("state_abc")
    assert entry is not None, "consume returned None for valid state"
    assert entry["csm_id"] == "user_xyz"
    assert entry["provider"] == "google"
    print("✓ save + consume within TTL → entry returned")


def test_get_state_with_status_ok():
    _reset_table()
    save_state("state_ok", "hubspot", "user_1", code_verifier="verifier_1")
    entry, status = get_state_with_status("state_ok")
    assert status == "ok", f"expected 'ok', got {status!r}"
    assert entry is not None and entry["csm_id"] == "user_1"
    assert entry["code_verifier"] == "verifier_1"
    print("✓ get_state_with_status within TTL → status='ok'")


def test_get_state_with_status_expired():
    _reset_table()
    save_state("state_expired", "google", "user_2")
    # Manipulate the row directly to simulate expiry (determinism without time travel)
    session = _TestSession()
    try:
        row = session.query(OAuthState).filter_by(state="state_expired").first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        session.commit()
    finally:
        session.close()

    entry, status = get_state_with_status("state_expired")
    assert status == "expired", f"expected 'expired', got {status!r}"
    assert entry is None
    print("✓ get_state_with_status past TTL → status='expired'")


def test_get_state_with_status_not_found():
    _reset_table()
    entry, status = get_state_with_status("state_never_existed")
    assert status == "not_found", f"expected 'not_found', got {status!r}"
    assert entry is None
    print("✓ get_state_with_status unknown state → status='not_found'")


def test_consume_expired_cleans_up_row():
    _reset_table()
    save_state("state_exp_cleanup", "gainsight", "user_3")
    session = _TestSession()
    try:
        row = session.query(OAuthState).filter_by(state="state_exp_cleanup").first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        session.commit()
    finally:
        session.close()

    result = consume_state("state_exp_cleanup")
    assert result is None, "consume of expired state should return None"
    # Verify row was deleted
    session = _TestSession()
    try:
        remaining = session.query(OAuthState).filter_by(state="state_exp_cleanup").first()
        assert remaining is None, "expired row should be deleted on consume"
    finally:
        session.close()
    print("✓ consume expired state → None AND row deleted")


def test_consume_twice_returns_not_found_on_second_call():
    """Replay-attack-protection invariant: one-time consumption."""
    _reset_table()
    save_state("state_once", "google", "user_4")
    first = consume_state("state_once")
    assert first is not None, "first consume should succeed"
    assert first["csm_id"] == "user_4"

    # Second consume — row should be gone
    second = consume_state("state_once")
    assert second is None, "second consume should return None (row deleted)"

    # And get_state_with_status should report not_found
    entry, status = get_state_with_status("state_once")
    assert status == "not_found", f"expected 'not_found' after consume, got {status!r}"
    assert entry is None
    print("✓ consume twice → second call is not_found (replay protection holds)")


def test_duration_log_fires_on_consume_ok():
    _reset_table()
    save_state("state_log_ok", "google", "user_5")

    records = []
    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    log_handler = _Capture()
    target = logging.getLogger("cs-agent.oauth_state_store")
    target.setLevel(logging.INFO)
    target.addHandler(log_handler)
    try:
        consume_state("state_log_ok")
    finally:
        target.removeHandler(log_handler)

    info_msgs = [r.getMessage() for r in records if r.levelname == "INFO"]
    assert any("OAuth state consumed" in m for m in info_msgs), \
        f"no consume INFO log fired; records: {info_msgs}"
    assert any("provider=google" in m and "status=ok" in m and "elapsed_seconds=" in m
               for m in info_msgs), \
        f"log missing expected fields; records: {info_msgs}"
    print(f"✓ consume ok logs duration+status: {[m for m in info_msgs if 'OAuth state' in m][0]!r}")


def test_duration_log_fires_on_consume_expired():
    _reset_table()
    save_state("state_log_exp", "hubspot", "user_6")
    session = _TestSession()
    try:
        row = session.query(OAuthState).filter_by(state="state_log_exp").first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=7)
        session.commit()
    finally:
        session.close()

    records = []
    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    log_handler = _Capture()
    target = logging.getLogger("cs-agent.oauth_state_store")
    target.setLevel(logging.INFO)
    target.addHandler(log_handler)
    try:
        consume_state("state_log_exp")
    finally:
        target.removeHandler(log_handler)

    info_msgs = [r.getMessage() for r in records if r.levelname == "INFO"]
    assert any("status=expired" in m and "elapsed_seconds=" in m and "provider=hubspot" in m
               for m in info_msgs), \
        f"expired log missing expected fields; records: {info_msgs}"
    print(f"✓ consume expired logs duration+status: {[m for m in info_msgs if 'OAuth state' in m][0]!r}")


def test_duration_log_fires_on_consume_not_found():
    _reset_table()
    records = []
    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    log_handler = _Capture()
    target = logging.getLogger("cs-agent.oauth_state_store")
    target.setLevel(logging.INFO)
    target.addHandler(log_handler)
    try:
        consume_state("state_never_existed")
    finally:
        target.removeHandler(log_handler)

    info_msgs = [r.getMessage() for r in records if r.levelname == "INFO"]
    assert any("status=not_found" in m for m in info_msgs), \
        f"not_found log missing; records: {info_msgs}"
    print("✓ consume not_found logs status=not_found")


def test_get_state_legacy_api_returns_none_for_expired():
    """Back-compat: legacy get_state() collapses expired+not_found to None."""
    _reset_table()
    save_state("state_legacy", "google", "user_7")
    session = _TestSession()
    try:
        row = session.query(OAuthState).filter_by(state="state_legacy").first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        session.commit()
    finally:
        session.close()
    assert get_state("state_legacy") is None
    assert get_state("state_never_existed") is None
    print("✓ legacy get_state() returns None for both expired and not_found")


def main():
    tests = [
        test_ttl_is_thirty_minutes,
        test_save_and_consume_within_ttl,
        test_get_state_with_status_ok,
        test_get_state_with_status_expired,
        test_get_state_with_status_not_found,
        test_consume_expired_cleans_up_row,
        test_consume_twice_returns_not_found_on_second_call,
        test_duration_log_fires_on_consume_ok,
        test_duration_log_fires_on_consume_expired,
        test_duration_log_fires_on_consume_not_found,
        test_get_state_legacy_api_returns_none_for_expired,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
            failures.append(t.__name__)
        except Exception as e:
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
            failures.append(t.__name__)
    print()
    if failures:
        print(f"FAILED: {len(failures)} of {len(tests)}")
        for n in failures:
            print(f"  - {n}")
        sys.exit(1)
    print(f"PASSED: {len(tests)} of {len(tests)}")


if __name__ == "__main__":
    main()
