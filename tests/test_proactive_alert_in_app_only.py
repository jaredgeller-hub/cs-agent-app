"""
tests/test_proactive_alert_in_app_only.py

F1 proactive-alert path is in-app-only in production: send_proactive_alert
records the ProactiveAlert row + disk artifact but sends NO email and loads
NO Google credentials when called with send_email=False (the live F1 loop).

Why this matters: the in-app alert surface (/app/alerts) must not depend on
the CSM's Google token. Before this change, F1 loaded creds at the top of
send_proactive_alert, so an expired/missing Google token would raise before
the row was ever written. send_email=False decouples the in-app row from
Google entirely.

Core assertions:
  - send_email=False: _load_credentials is NOT called (monkeypatched to raise),
    status == "in_app", a ProactiveAlert row is written, the disk artifact is
    written, and Gmail build/send never happen.
  - send_email=True (default): existing send behavior is preserved (status "sent").
  - duplicate dedup key still returns "duplicate_suppressed".
  - the live F1 loop call site (main.py) passes send_email=False.

Usage:
    python3 -m pytest tests/test_proactive_alert_in_app_only.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import Base, Connection, Customer, ProactiveAlert
from tests._utils import seed_customer


# ---------------------------------------------------------------------------
# Test database setup (mirrors tests/test_absence_detection.py)
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

    send_proactive_alert does `from product.database import SessionLocal`
    at call time, so rebinding the module attribute routes its insert to
    the in-memory test engine.
    """
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_tables():
    s = _TestSession()
    try:
        s.query(ProactiveAlert).delete()
        s.query(Connection).delete()
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


def _signal(account="Meridian Health"):
    return {
        "thread_id": "thread_inapp",
        "contact": "marcus@meridian.example",
        "account": account,
        "detail": "5 days since last reply",
        "renewal_days": 60,
        "signal_type": "absence",
    }


# ---------------------------------------------------------------------------
# send_email=False — in-app only, no Google dependency
# ---------------------------------------------------------------------------

def test_in_app_only_does_not_load_credentials(tmp_path, monkeypatch):
    """send_email=False must NOT load Google creds or build/send Gmail, yet
    must still write the ProactiveAlert row and the disk artifact."""
    from product.digest import delivery

    csm_id = "user_inapp_only"
    s = _TestSession()
    try:
        seed_customer(s, csm_id)
    finally:
        s.close()

    # Route the disk artifact to a temp dir so the test never touches the repo.
    monkeypatch.setattr(delivery, "DATA_DIR", tmp_path)

    # Prove no Google path is taken: both blow up if reached.
    def _boom_creds(_csm_id):
        raise AssertionError("_load_credentials must not be called when send_email=False")

    build_mock = MagicMock(side_effect=AssertionError("build() must not be called when send_email=False"))

    with patch("product.digest.delivery._load_credentials", side_effect=_boom_creds), \
         patch("product.digest.delivery.build", build_mock):
        result = delivery.send_proactive_alert(
            signal=_signal(),
            urgency_score=0.85,
            csm_id=csm_id,
            csm_email="csm@example.com",
            suggested_draft="",
            signal_id="thread_inapp:marcus@meridian.example:Meridian Health",
            send_email=False,
        )

    # Return shape
    assert result["status"] == "in_app"
    assert result["message_id"] == ""
    assert result["sent_at"]

    # Gmail service was never constructed → no send occurred.
    assert not build_mock.called

    # ProactiveAlert row written with idempotency fields populated.
    s = _TestSession()
    try:
        rows = s.query(ProactiveAlert).filter_by(csm_id=csm_id).all()
        assert len(rows) == 1
        assert rows[0].signal_id == "thread_inapp:marcus@meridian.example:Meridian Health"
        assert rows[0].alert_date == datetime.now(timezone.utc).date()
        assert rows[0].account == "Meridian Health"
    finally:
        s.close()

    # Disk artifact written for in-app polling.
    artifacts = list((tmp_path / "alerts" / csm_id).glob("*.json"))
    assert len(artifacts) == 1, f"expected exactly one disk artifact, got {artifacts}"
    assert "needs your attention" in artifacts[0].read_text(encoding="utf-8")


def test_in_app_only_writes_row_even_with_no_db_creds(tmp_path, monkeypatch):
    """Even if SessionLocal is None (DB not configured), send_email=False
    still writes the disk artifact and returns in_app without touching Google."""
    from product.digest import delivery

    monkeypatch.setattr(delivery, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_db, "SessionLocal", None)

    with patch("product.digest.delivery._load_credentials",
               side_effect=AssertionError("_load_credentials must not be called")):
        result = delivery.send_proactive_alert(
            signal=_signal("Beacon Financial"),
            urgency_score=0.9,
            csm_id="user_nodb",
            csm_email="csm@example.com",
            suggested_draft="",
            signal_id="sid_nodb",
            send_email=False,
        )

    assert result["status"] == "in_app"
    artifacts = list((tmp_path / "alerts" / "user_nodb").glob("*.json"))
    assert len(artifacts) == 1


# ---------------------------------------------------------------------------
# send_email=True (default) — regression: existing send behavior preserved
# ---------------------------------------------------------------------------

def test_default_send_email_true_still_sends(tmp_path, monkeypatch):
    """Default call (no send_email kwarg) keeps sending email: creds loaded,
    Gmail send invoked, status 'sent'."""
    from product.digest import delivery

    csm_id = "user_send_default"
    s = _TestSession()
    try:
        seed_customer(s, csm_id)
    finally:
        s.close()

    monkeypatch.setattr(delivery, "DATA_DIR", tmp_path)

    mock_service = MagicMock()
    mock_service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "msg_default_123"
    }

    with patch("product.digest.delivery._load_credentials", return_value=MagicMock()), \
         patch("product.digest.delivery.build", return_value=mock_service):
        result = delivery.send_proactive_alert(
            signal=_signal(),
            urgency_score=0.85,
            csm_id=csm_id,
            csm_email="csm@example.com",
            suggested_draft="",
            signal_id="sid_default",
        )

    assert result["status"] == "sent"
    assert result["message_id"] == "msg_default_123"
    assert mock_service.users.return_value.messages.return_value.send.called


# ---------------------------------------------------------------------------
# Dedup still works in in-app mode
# ---------------------------------------------------------------------------

def test_duplicate_suppressed_in_app(tmp_path, monkeypatch):
    """Two in-app calls with the same (csm, account, signal_id, date) →
    second is duplicate_suppressed, no second row."""
    from product.digest import delivery

    csm_id = "user_inapp_dup"
    s = _TestSession()
    try:
        seed_customer(s, csm_id)
    finally:
        s.close()

    monkeypatch.setattr(delivery, "DATA_DIR", tmp_path)

    kwargs = dict(
        signal=_signal("Northgate Logistics"),
        urgency_score=0.9,
        csm_id=csm_id,
        csm_email="csm@example.com",
        suggested_draft="",
        signal_id="sid_dup_inapp",
        send_email=False,
    )

    with patch("product.digest.delivery._load_credentials",
               side_effect=AssertionError("_load_credentials must not be called")):
        first = delivery.send_proactive_alert(**kwargs)
        second = delivery.send_proactive_alert(**kwargs)

    assert first["status"] == "in_app"
    assert second["status"] == "duplicate_suppressed"
    assert second["signal_id"] == "sid_dup_inapp"

    s = _TestSession()
    try:
        assert s.query(ProactiveAlert).filter_by(csm_id=csm_id).count() == 1
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Wiring guard: the live F1 loop call site passes send_email=False
# ---------------------------------------------------------------------------
#
# A full _run_absence_detection loop test would need the broad org_config /
# CRM / Gmail / SMTP mock stack already maintained in
# tests/test_absence_detection.py (_patch_loop_environment). Rather than
# duplicate that machinery here, this focused source-scan guards the one
# wiring fact that matters: the live F1 branch calls send_proactive_alert
# with send_email=False. The delivery-level tests above are the behavioral
# core.

def test_f1_loop_call_site_passes_send_email_false():
    main_src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

    # `send_proactive_alert(` only appears at the call site (the import line
    # has no paren). Scan the multi-line keyword-arg block that follows it;
    # a window comfortably covers the call without tripping on the nested
    # `derive_gmail_signal_id(signal)` paren.
    idx = main_src.find("send_proactive_alert(")
    assert idx != -1, "send_proactive_alert call not found in main.py"
    window = main_src[idx:idx + 500]
    assert "send_email=False" in window, (
        "live F1 loop must call send_proactive_alert(send_email=False) — "
        "the F1 path is in-app only and must not send email"
    )
