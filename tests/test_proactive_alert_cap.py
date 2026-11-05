"""
tests/test_proactive_alert_cap.py

WS0.5 — per-CSM daily proactive-alert cap.

Covers main._proactive_alert_cap_reached and its enforcement inside
main._write_urgency_alerts (Path B). The cap is a SHARED budget across both
alert paths — F1 absence (Path A, via send_proactive_alert) and Sprint 6
urgency (Path B) — because both stamp alert_date = today UTC and the helper
counts by that column. The shared-budget test proves pre-existing F1-style
rows count against the urgency path's writes.

Isolation: in-memory SQLite (StaticPool so the per-account SessionLocal()
opened inside the function shares one connection), Base.metadata.create_all
(builds the proactive_alerts_dedup_unique partial index), SessionLocal
monkey-patched with the autouse re-bind fixture required by the CLAUDE.md
invariant. Same pattern as tests/test_write_urgency_alerts.py.

Usage:
    python3 -m pytest tests/test_proactive_alert_cap.py -v
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import Base, ProactiveAlert

# conftest.py sets TOKEN_ENCRYPTION_KEY before collection so importing main
# (which imports product.token_crypto) succeeds here.
from main import (
    MAX_PROACTIVE_ALERTS_PER_DAY,
    _proactive_alert_cap_reached,
    _write_urgency_alerts,
)


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
def _reset_tables():
    s = _TestSession()
    try:
        s.query(ProactiveAlert).delete()
        s.commit()
    finally:
        s.close()
    yield


# ── Helpers ─────────────────────────────────────────────────────────────────

def _insert_alert(csm_id: str, account: str, signal_id: str, alert_date):
    """Insert a ProactiveAlert row directly (simulates an already-written
    alert, e.g. an F1 absence row) so we can exercise the shared budget."""
    s = _TestSession()
    try:
        s.add(ProactiveAlert(
            id=str(uuid.uuid4()),
            csm_id=csm_id,
            account=account,
            alert_text=f"{account} needs attention",
            draft_text="",
            urgency="0.90",
            signal_id=signal_id,
            alert_date=alert_date,
        ))
        s.commit()
    finally:
        s.close()


def _count_today(csm_id: str) -> int:
    s = _TestSession()
    try:
        return (
            s.query(ProactiveAlert)
            .filter(ProactiveAlert.csm_id == csm_id)
            .filter(ProactiveAlert.alert_date == datetime.now(timezone.utc).date())
            .count()
        )
    finally:
        s.close()


def _urgency_payload(n: int) -> dict:
    """n distinct at_risk accounts with descending score."""
    return {
        f"Account {i:02d}": {
            "score": 100 - i,
            "risk": "at_risk",
            "drivers": [f"driver {i}"],
        }
        for i in range(n)
    }


# ── Cap enforcement ─────────────────────────────────────────────────────────

def test_cap_enforced_at_exactly_8():
    """10 at_risk accounts in one call → exactly MAX_PROACTIVE_ALERTS_PER_DAY
    (8) rows written; the 9th and 10th are skipped."""
    csm_id = "user_cap8"
    written = _write_urgency_alerts(csm_id, _urgency_payload(10))
    assert written == MAX_PROACTIVE_ALERTS_PER_DAY == 8
    assert _count_today(csm_id) == 8


def test_ninth_write_skipped_when_budget_prefilled():
    """With 8 alerts already recorded today, a further urgency write is
    skipped entirely (0 written)."""
    csm_id = "user_prefilled"
    for i in range(8):
        _insert_alert(csm_id, f"F1 {i}", f"gmail:{i}", datetime.now(timezone.utc).date())
    assert _proactive_alert_cap_reached(csm_id) is True

    written = _write_urgency_alerts(csm_id, _urgency_payload(3))
    assert written == 0
    assert _count_today(csm_id) == 8  # unchanged


def test_highest_score_kept_when_capped():
    """When the cap truncates, the WITHIN-path ordering keeps the
    highest-score accounts. Account 00 (score 100) must be written; the
    lowest-score account must not be."""
    csm_id = "user_order"
    _write_urgency_alerts(csm_id, _urgency_payload(10))
    s = _TestSession()
    try:
        accounts = {
            a.account for a in
            s.query(ProactiveAlert).filter(ProactiveAlert.csm_id == csm_id).all()
        }
    finally:
        s.close()
    assert "Account 00" in accounts       # score 100 — highest
    assert "Account 09" not in accounts   # score 91 — lowest, dropped


# ── New-day reset ───────────────────────────────────────────────────────────

def test_resets_on_new_utc_day():
    """8 alerts stamped YESTERDAY do not count against today's budget —
    today's urgency writes proceed."""
    csm_id = "user_newday"
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    for i in range(8):
        _insert_alert(csm_id, f"Old {i}", f"gmail:{i}", yesterday)

    assert _proactive_alert_cap_reached(csm_id) is False  # today's count is 0
    written = _write_urgency_alerts(csm_id, _urgency_payload(2))
    assert written == 2
    assert _count_today(csm_id) == 2


# ── Shared budget across Path A + Path B ────────────────────────────────────

def test_shared_budget_absence_plus_urgency():
    """5 F1-style absence rows already today + an urgency call for 5 accounts
    → only 3 urgency rows written (5 + 3 = 8). Proves ONE shared budget, not
    two separate per-path budgets."""
    csm_id = "user_shared"
    for i in range(5):
        _insert_alert(csm_id, f"F1 {i}", f"gmail:{i}", datetime.now(timezone.utc).date())

    written = _write_urgency_alerts(csm_id, _urgency_payload(5))
    assert written == 3, "urgency path must respect alerts already written by Path A"
    assert _count_today(csm_id) == 8


# ── Log line ────────────────────────────────────────────────────────────────

def test_cap_reached_emits_structured_log(caplog):
    csm_id = "user_log"
    for i in range(8):
        _insert_alert(csm_id, f"F1 {i}", f"gmail:{i}", datetime.now(timezone.utc).date())

    caplog.set_level(logging.INFO)
    _write_urgency_alerts(csm_id, _urgency_payload(1))

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "event=alert_cap_reached" in m
        and f"csm_id={csm_id}" in m
        and "cap=8" in m
        and "skipped_path=urgency" in m
        for m in msgs
    ), msgs


# ── Fail-open ───────────────────────────────────────────────────────────────

def test_cap_helper_fails_open_without_db():
    """No DB → cap never engages (fail-open: a hiccup must not silence
    every alert)."""
    original = _db.SessionLocal
    _db.SessionLocal = None
    try:
        assert _proactive_alert_cap_reached("anyone") is False
    finally:
        _db.SessionLocal = original


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
