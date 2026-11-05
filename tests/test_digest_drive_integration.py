"""
tests/test_digest_drive_integration.py

Integration smoke tests for Drive transcripts wired into the digest
pipeline (Sprint 1, Item 2.1).

The digest pipeline (`_run_digest_for_user`) had zero tests before this
commit. These tests are the safety net: they confirm the Drive call is
gated correctly for sarah_c, that error / empty / partial results don't
break the pipeline, and that the new detect_and_classify drive_signals
parameter flows correctly.

Standalone-runnable:
    python3 tests/test_digest_drive_integration.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("STRIPE_PRICE_ID", "price_test_fixture")

import pytest

from product.signals.detector import detect_and_classify


# ── detect_and_classify: drive_signals parameter ─────────────────────────────


def _empty_calendar():
    return {
        "customer_meetings": [], "internal_meetings": [],
        "total_customer_meetings": 0, "total_meetings": 0,
    }


def test_detect_and_classify_backward_compat_without_drive_signals():
    """All existing call sites still work with the original 5-arg shape."""
    result = detect_and_classify([], _empty_calendar(), [], {})
    assert "priority_accounts" in result
    assert result["priority_accounts"] == []


def test_detect_and_classify_drive_signals_promote_account():
    """An account with only a drive transcript still appears in priority_accounts."""
    drive = {
        "Northgate Logistics": [
            {"modified_time": "2026-04-28T11:00:00Z",
             "content_snippet": "Discussed Q2 health metrics and renewal."}
        ]
    }
    result = detect_and_classify([], _empty_calendar(), [], {}, drive_signals=drive)
    assert any(a["account"] == "Northgate Logistics" for a in result["priority_accounts"])
    acct = next(a for a in result["priority_accounts"] if a["account"] == "Northgate Logistics")
    drive_signals = [s for s in acct["signals"] if s.get("type") == "drive.transcript"]
    assert len(drive_signals) == 1
    assert drive_signals[0]["classification"] == "NORMAL"
    assert "2026-04-28" in drive_signals[0]["detail"]


def test_detect_and_classify_empty_drive_signals_is_safe():
    """drive_signals={} or None must not break the pipeline."""
    r1 = detect_and_classify([], _empty_calendar(), [], {}, drive_signals=None)
    r2 = detect_and_classify([], _empty_calendar(), [], {}, drive_signals={})
    assert r1["priority_accounts"] == []
    assert r2["priority_accounts"] == []


# ── _run_digest_for_user: Drive call gating ─────────────────────────────────


def _build_main_test_fixtures(monkeypatch):
    """Patch out network-touching helpers so _run_digest_for_user is a
    pure-Python smoke. Returns the find_meet_transcripts mock so tests
    can assert call_count."""
    import main

    # Stub out everything that would touch the network or DB.
    monkeypatch.setattr("product.signals.gmail.extract_gmail_signals", lambda *a, **kw: [])
    monkeypatch.setattr("product.signals.calendar.extract_calendar_signals",
                        lambda *a, **kw: _empty_calendar())
    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk", lambda *a, **kw: None)
    monkeypatch.setattr("product.digest.delivery.send_notification_email",
                        lambda *a, **kw: None)
    # Skip Postgres save and the Redis pending-key clear inside the pipeline.
    monkeypatch.setattr(main, "_digest_in_flight", {})

    # The pipeline calls a real LLM via build_morning_digest. Replace
    # the entire builder with a canned dict to avoid the API call.
    monkeypatch.setattr(
        "product.digest.builder.build_morning_digest",
        lambda classified, csm_name, **kw: {
            "date":         "2026-05-05",
            "greeting":     f"Good morning, {csm_name}.",
            "digest_text":  "Test digest output.",
            "accounts_mentioned": [],
            "action_items": [],
            "meetings_today":     [],
            # Capture drive_signals for assertion
            "_kw_drive_signals": kw.get("drive_signals"),
        },
    )

    # find_meet_transcripts is async; replace with a sync-callable async stub
    fmt = MagicMock()
    monkeypatch.setattr("product.signals.drive.find_meet_transcripts", fmt)
    return fmt


def test_drive_call_skipped_for_sarah_c(monkeypatch):
    """sarah_c demo path must NOT call find_meet_transcripts."""
    fmt = _build_main_test_fixtures(monkeypatch)

    # Provide minimal config for sarah_c
    monkeypatch.setattr("product.org_config.get_csm_config", lambda csm_id: {
        "name": "Sarah", "email": "s@example.com",
        "accounts": ["Northgate"], "timezone": "America/Chicago",
        "notifications": {"email": False},
    })
    monkeypatch.setattr("product.org_config.get_pilot_mode", lambda: "shadow")
    monkeypatch.setattr("product.org_config.get_crm_config", lambda: {})

    import main
    main._run_digest_for_user("sarah_c")

    fmt.assert_not_called()


def test_drive_call_made_for_clerk_user(monkeypatch):
    """Non-sarah_c users get the Drive call, with rebound semaphore."""
    import asyncio as _aio
    fmt = _build_main_test_fixtures(monkeypatch)

    async def fake_find(csm_id, days=7):
        return [{
            "file_id": "f1",
            "title": "Meeting transcript - 2026-05-04 - Northgate",
            "modified_time": "2026-05-04T11:00:00Z",
            "content_snippet": "Discussed Q2",
            "created_time": "2026-05-04T10:00:00Z",
            "web_link": "",
        }]
    fmt.side_effect = fake_find

    # Mock the database.Customer query path that user_xxx triggers
    fake_cust = MagicMock()
    fake_cust.name = "Test User"
    fake_cust.email = "t@x.example"
    fake_cust.accounts = ["Northgate"]
    fake_db = MagicMock()
    fake_db.query.return_value.filter_by.return_value.first.return_value = fake_cust
    fake_session_factory = MagicMock(return_value=fake_db)

    monkeypatch.setattr("product.database.SessionLocal", fake_session_factory)
    monkeypatch.setattr("product.org_config.get_pilot_mode", lambda: "shadow")
    monkeypatch.setattr("product.org_config.get_crm_config", lambda: {})

    import main
    main._run_digest_for_user("user_test123")

    fmt.assert_called_once()


def test_drive_call_failure_does_not_break_digest(monkeypatch):
    """find_meet_transcripts raising must not propagate; digest continues."""
    import asyncio as _aio
    fmt = _build_main_test_fixtures(monkeypatch)

    async def boom(csm_id, days=7):
        raise RuntimeError("drive api down")
    fmt.side_effect = boom

    fake_cust = MagicMock()
    fake_cust.name = "Test"
    fake_cust.email = "t@x.example"
    fake_cust.accounts = ["Northgate"]
    fake_db = MagicMock()
    fake_db.query.return_value.filter_by.return_value.first.return_value = fake_cust
    monkeypatch.setattr("product.database.SessionLocal", MagicMock(return_value=fake_db))
    monkeypatch.setattr("product.org_config.get_pilot_mode", lambda: "shadow")
    monkeypatch.setattr("product.org_config.get_crm_config", lambda: {})

    import main
    # Should NOT raise
    main._run_digest_for_user("user_test123")

    fmt.assert_called_once()


def test_drive_results_grouped_by_title_to_account(monkeypatch):
    """Transcripts whose titles match account names flow into drive_by_account
    grouping passed to build_morning_digest."""
    captured = {}
    fmt = _build_main_test_fixtures(monkeypatch)

    async def fake_find(csm_id, days=7):
        return [
            {"file_id": "f1", "title": "Meeting transcript - 2026-05-04 - Northgate Logistics",
             "modified_time": "2026-05-04T11:00:00Z", "content_snippet": "renewal",
             "created_time": "", "web_link": ""},
            {"file_id": "f2", "title": "Meeting transcript - 2026-05-03 - Beacon Financial",
             "modified_time": "2026-05-03T11:00:00Z", "content_snippet": "QBR",
             "created_time": "", "web_link": ""},
            {"file_id": "f3", "title": "Internal team sync",
             "modified_time": "2026-05-02T11:00:00Z", "content_snippet": "",
             "created_time": "", "web_link": ""},
        ]
    fmt.side_effect = fake_find

    def capturing_builder(classified, csm_name, **kw):
        captured["drive_signals"] = kw.get("drive_signals")
        return {"date": "2026-05-05", "digest_text": "x", "greeting": "Hi",
                "accounts_mentioned": [], "action_items": [], "meetings_today": []}

    monkeypatch.setattr("product.digest.builder.build_morning_digest", capturing_builder)

    fake_cust = MagicMock()
    fake_cust.name = "Test"
    fake_cust.email = "t@x.example"
    fake_cust.accounts = ["Northgate Logistics", "Beacon Financial"]
    fake_db = MagicMock()
    fake_db.query.return_value.filter_by.return_value.first.return_value = fake_cust
    monkeypatch.setattr("product.database.SessionLocal", MagicMock(return_value=fake_db))
    monkeypatch.setattr("product.org_config.get_pilot_mode", lambda: "shadow")
    monkeypatch.setattr("product.org_config.get_crm_config", lambda: {})

    import main
    main._run_digest_for_user("user_test123")

    drive_signals = captured["drive_signals"]
    assert drive_signals is not None
    assert "Northgate Logistics" in drive_signals
    assert "Beacon Financial" in drive_signals
    # The unmatched "Internal team sync" must not appear under any account
    all_file_ids = {t["file_id"] for vs in drive_signals.values() for t in vs}
    assert "f3" not in all_file_ids


# ── Builder TRANSCRIPTS context block ────────────────────────────────────────


def test_build_context_emits_transcripts_block_when_drive_signals_present():
    from product.digest.builder import _build_context
    classified = {"priority_accounts": [], "todays_meetings": [], "overdue_ctas": [],
                  "suggested_focus": ""}
    drive = {
        "Northgate Logistics": [
            {"modified_time": "2026-04-28T11:00:00Z",
             "content_snippet": "Discussed Q2 health metrics."}
        ]
    }
    ctx = _build_context(classified, crm_signals=None, drive_signals=drive)
    assert "=== TRANSCRIPTS ===" in ctx
    assert "Northgate Logistics:" in ctx
    assert "2026-04-28" in ctx


def test_build_context_omits_transcripts_block_when_no_drive_signals():
    from product.digest.builder import _build_context
    classified = {"priority_accounts": [], "todays_meetings": [], "overdue_ctas": [],
                  "suggested_focus": ""}
    ctx = _build_context(classified, crm_signals=None, drive_signals=None)
    assert "=== TRANSCRIPTS ===" not in ctx
    ctx2 = _build_context(classified, crm_signals=None, drive_signals={})
    assert "=== TRANSCRIPTS ===" not in ctx2


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v", "--timeout=30"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
