"""tests/test_digest_single_refresh.py

Stage 4 single-refresh hoist tests.

Eliminates the cold-start double CRM pull in the morning digest by
refreshing the context cache ONCE in _run_digest_for_user and having
score_account_urgency skip its own refresh (via the new refresh=False
parameter, threaded through _build_new_digest_for_user's
skip_urgency_refresh kwarg).

Default behavior of score_account_urgency MUST stay identical for the
F1 absence loop and /admin/trigger-absence. Only the digest helper may
opt out of the refresh.

Assertions are verification-evidence: refresh_user_context call_count
is asserted explicitly so a regression to the double-pull is caught.

Standalone-runnable:
    python3 tests/test_digest_single_refresh.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("STRIPE_PRICE_ID", "price_test_fixture")

import pytest

import main
from product.signals.detector import score_account_urgency


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cached(accounts=None, deals=None, health=None, tickets=None):
    """Build the dict shape get_cached_context() returns."""
    data = {
        "accounts":      accounts or [],
        "deals":         deals    or [],
        "health_scores": health   or [],
        "tickets":       tickets  or [],
    }
    return {
        "data":           data,
        "refreshed_at":   datetime.now(timezone.utc),
        "refresh_status": "complete",
        "source_status":  {},
    }


class _RefreshCounter:
    """Async callable that counts invocations of refresh_user_context."""

    def __init__(self, raises: Exception | None = None):
        self.call_count = 0
        self.csm_ids: list[str] = []
        self._raises = raises

    async def __call__(self, csm_id):
        self.call_count += 1
        self.csm_ids.append(csm_id)
        if self._raises is not None:
            raise self._raises
        return None


# ────────────────────────────────────────────────────────────────────────────
# (a) score_account_urgency(refresh=False) does NOT invoke refresh_user_context
# ────────────────────────────────────────────────────────────────────────────

def test_score_account_urgency_refresh_false_skips_refresh(monkeypatch):
    """refresh=False: refresh_user_context call_count == 0; cached snapshot
    still read and scored."""
    counter = _RefreshCounter()
    monkeypatch.setattr("product.context_cache.refresh_user_context", counter)
    monkeypatch.setattr(
        "product.context_cache.get_cached_context",
        lambda csm_id: _cached(
            accounts=[{"name": "Acme"}],
            deals=[{"account_name": "Acme", "days_until_close": 25}],  # +30
        ),
    )
    monkeypatch.setattr("product.signal_cache.read_signals",
                        lambda *a, **kw: [])

    out = score_account_urgency("user_test", refresh=False)

    assert counter.call_count == 0, (
        f"refresh=False must NOT trigger refresh_user_context "
        f"(actual call_count={counter.call_count})"
    )
    assert out["Acme"]["score"] == 30
    assert "Renewal in 25 days" in out["Acme"]["drivers"]


# ────────────────────────────────────────────────────────────────────────────
# (b) score_account_urgency() with default (no kwarg) DOES refresh
#     This is the F1/admin safeguard.
# ────────────────────────────────────────────────────────────────────────────

def test_score_account_urgency_default_still_refreshes(monkeypatch):
    """No kwarg passed -> default refresh=True -> refresh_user_context
    invoked exactly once. The F1 absence loop and /admin/trigger-absence
    depend on this."""
    counter = _RefreshCounter()
    monkeypatch.setattr("product.context_cache.refresh_user_context", counter)
    monkeypatch.setattr(
        "product.context_cache.get_cached_context",
        lambda csm_id: _cached(accounts=[{"name": "Acme"}]),
    )
    monkeypatch.setattr("product.signal_cache.read_signals",
                        lambda *a, **kw: [])

    out = score_account_urgency("user_test")

    assert counter.call_count == 1, (
        f"default behavior must force-refresh exactly once "
        f"(actual call_count={counter.call_count}); the F1 absence loop "
        f"and admin trigger rely on this"
    )
    assert counter.csm_ids == ["user_test"]
    assert out == {"Acme": {"score": 0, "risk": "on_track", "drivers": []}}


def test_score_account_urgency_explicit_refresh_true_refreshes(monkeypatch):
    """Explicit refresh=True behaves the same as the default."""
    counter = _RefreshCounter()
    monkeypatch.setattr("product.context_cache.refresh_user_context", counter)
    monkeypatch.setattr(
        "product.context_cache.get_cached_context",
        lambda csm_id: _cached(accounts=[{"name": "Acme"}]),
    )
    monkeypatch.setattr("product.signal_cache.read_signals",
                        lambda *a, **kw: [])

    score_account_urgency("user_test", refresh=True)

    assert counter.call_count == 1


def test_score_account_urgency_default_propagates_refresh_failure(monkeypatch):
    """Fail-closed contract preserved on the default path: if the refresh
    raises, the function re-raises (it does NOT silently fall through to
    stale cache)."""
    counter = _RefreshCounter(raises=RuntimeError("simulated refresh failure"))
    monkeypatch.setattr("product.context_cache.refresh_user_context", counter)
    monkeypatch.setattr(
        "product.context_cache.get_cached_context",
        lambda csm_id: _cached(accounts=[{"name": "Acme"}]),
    )
    monkeypatch.setattr("product.signal_cache.read_signals",
                        lambda *a, **kw: [])

    with pytest.raises(RuntimeError, match="simulated refresh failure"):
        score_account_urgency("user_test")
    assert counter.call_count == 1


def test_score_account_urgency_refresh_false_does_not_swallow_get_cached(monkeypatch):
    """refresh=False still goes through validate_csm_id and reads cache
    via get_cached_context. An empty cache returns {}; we are NOT bypassing
    the normal cache-read path."""
    counter = _RefreshCounter()
    monkeypatch.setattr("product.context_cache.refresh_user_context", counter)
    monkeypatch.setattr(
        "product.context_cache.get_cached_context",
        lambda csm_id: None,
    )
    monkeypatch.setattr("product.signal_cache.read_signals",
                        lambda *a, **kw: [])

    out = score_account_urgency("user_test", refresh=False)
    assert out == {}
    assert counter.call_count == 0


# ────────────────────────────────────────────────────────────────────────────
# (c) _build_new_digest_for_user threads skip_urgency_refresh -> refresh kwarg
# ────────────────────────────────────────────────────────────────────────────

def _patch_helper_deps(monkeypatch):
    """Stub the in-helper imports of extract_calendar_range and the voice
    slots so _build_new_digest_for_user runs deterministically without
    network or Anthropic calls."""
    monkeypatch.setattr(
        "product.signals.calendar.extract_calendar_range",
        lambda csm_id, names, s, e: {
            "start_date": "2026-06-02", "end_date": "2026-06-08",
            "timezone": "America/Chicago", "by_date": {},
            "total_customer_meetings": 0, "total_meetings": 0,
        },
    )
    monkeypatch.setattr(
        "product.digest.generator._generate_voice_slots",
        lambda *a, **kw: {"priority_line": "P", "closing_line": "C"},
    )


def test_helper_skip_urgency_refresh_true_passes_refresh_false(monkeypatch):
    """_build_new_digest_for_user(..., skip_urgency_refresh=True) calls
    score_account_urgency with refresh=False."""
    _patch_helper_deps(monkeypatch)

    captured = {}

    def _fake_score(csm_id, refresh=True):
        captured["csm_id"] = csm_id
        captured["refresh"] = refresh
        return {"Acme": {"score": 0, "risk": "on_track", "drivers": []}}

    monkeypatch.setattr(
        "product.signals.detector.score_account_urgency", _fake_score,
    )

    import zoneinfo
    main._build_new_digest_for_user(
        csm_id="user_test",
        csm_name="Test",
        csm_tz=zoneinfo.ZoneInfo("America/Chicago"),
        crm_type="hubspot",
        account_names=["Acme"],
        crm_signals=[],
        calendar_signals={
            "date": "2026-06-02", "customer_meetings": [],
            "internal_meetings": [], "total_customer_meetings": 0,
            "total_meetings": 0, "available_gaps": [],
        },
        classified={},
        crm_fetch_status="ok",
        gmail_status="ok",
        calendar_status="ok",
        drive_status="empty",
        skip_urgency_refresh=True,
    )

    assert captured["refresh"] is False, (
        "skip_urgency_refresh=True must thread refresh=False to "
        f"score_account_urgency (got refresh={captured.get('refresh')!r})"
    )


def test_helper_default_skip_false_passes_refresh_true(monkeypatch):
    """_build_new_digest_for_user with no skip_urgency_refresh (the chat
    intercept path) calls score_account_urgency with refresh=True."""
    _patch_helper_deps(monkeypatch)

    captured = {}

    def _fake_score(csm_id, refresh=True):
        captured["refresh"] = refresh
        return {}

    monkeypatch.setattr(
        "product.signals.detector.score_account_urgency", _fake_score,
    )

    import zoneinfo
    main._build_new_digest_for_user(
        csm_id="user_test",
        csm_name="Test",
        csm_tz=zoneinfo.ZoneInfo("America/Chicago"),
        crm_type="hubspot",
        account_names=["Acme"],
        crm_signals=[],
        calendar_signals={
            "date": "2026-06-02", "customer_meetings": [],
            "internal_meetings": [], "total_customer_meetings": 0,
            "total_meetings": 0, "available_gaps": [],
        },
        classified={},
        crm_fetch_status="ok",
        gmail_status="ok",
        calendar_status="ok",
        drive_status="empty",
        # skip_urgency_refresh omitted -> defaults to False
    )

    assert captured["refresh"] is True, (
        "default skip_urgency_refresh=False must keep refresh=True "
        f"(got refresh={captured.get('refresh')!r}); chat-intercept path "
        f"must be byte-identical"
    )


# ────────────────────────────────────────────────────────────────────────────
# (d, e, f) Runner integration: total refresh count under flag, byte-identical
# legacy path, and failure-fallback to score_account_urgency self-refresh.
# ────────────────────────────────────────────────────────────────────────────

def _patch_runner_pipeline(monkeypatch, *,
                           owned_accounts: list[str],
                           urgency: dict | None = None,
                           crm_type: str = "hubspot"):
    """Stub every network or DB-touching helper inside _run_digest_for_user.

    Mirrors test_digest_wiring._patch_login_pipeline but provides a
    minimal shape. score_account_urgency is stubbed with a refresh
    counter so the integration assertion can read call_count directly.
    """
    fake_cfg = {
        "name": "Test User",
        "email": "test@example.com",
        "accounts": list(owned_accounts),
        "timezone": "America/Chicago",
        "notifications": {"email": False},
    }
    monkeypatch.setattr("product.org_config.get_csm_config",
                        lambda csm_id: fake_cfg)
    monkeypatch.setattr("product.org_config.get_crm_config",
                        lambda: {"crm_type": crm_type})
    monkeypatch.setattr("product.org_config.get_pilot_mode", lambda: "live")
    monkeypatch.setattr(main, "get_csm_timezone",
                        lambda csm_id: __import__("zoneinfo").ZoneInfo("America/Chicago"))

    # The cached snapshot is what the helper reads after the up-front
    # refresh. Return an empty snapshot so the crm_signals branch finds
    # nothing to iterate (we do not need crm_signals for these assertions).
    monkeypatch.setattr(
        "product.context_cache.get_cached_context",
        lambda csm_id: _cached(
            accounts=[{"name": n} for n in owned_accounts],
        ),
    )
    monkeypatch.setattr(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        lambda csm_id: None,
    )

    # Connector: not needed if get_cached_context returns a non-empty
    # snapshot, but provide a no-op to avoid live import paths.
    from product.connectors.source_status import SourceStatus as _SS
    fake_conn = MagicMock()
    fake_conn.get_accounts.return_value = [
        {"id": f"id_{i}", "hubspot_id": 1000 + i, "name": n,
         "tier": "standard", "owner": "test"}
        for i, n in enumerate(owned_accounts)
    ]
    fake_conn.get_accounts_status.return_value = _SS.OK
    fake_conn.get_contacts.return_value = []
    monkeypatch.setattr("product.connectors.registry.get_connector",
                        lambda csm_id, cfg: fake_conn)
    monkeypatch.setattr(
        "product.signals.detector.detect_crm_signals",
        lambda *a, **kw: [],
    )

    # Gmail, Calendar (today + week-ahead), Drive
    monkeypatch.setattr(
        "product.signals.gmail.extract_gmail_signals",
        lambda csm_id, ac: [],
    )
    monkeypatch.setattr(
        "product.signals.calendar.extract_calendar_signals",
        lambda csm_id, names: {
            "date": "2026-06-02", "customer_meetings": [],
            "internal_meetings": [], "total_customer_meetings": 0,
            "total_meetings": 0, "available_gaps": [],
        },
    )
    monkeypatch.setattr(
        "product.signals.calendar.extract_calendar_range",
        lambda csm_id, names, s, e: {
            "start_date": "2026-06-02", "end_date": "2026-06-08",
            "timezone": "America/Chicago", "by_date": {},
            "total_customer_meetings": 0, "total_meetings": 0,
        },
    )
    monkeypatch.setattr(
        "product.signals.drive.find_meet_transcripts",
        AsyncMock(return_value=[]),
    )

    # Voice slots, no LLM
    monkeypatch.setattr(
        "product.digest.generator._generate_voice_slots",
        lambda *a, **kw: {"priority_line": "P", "closing_line": "C"},
    )

    # Suppress downstream side effects
    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk",
                        lambda *a, **kw: None)
    monkeypatch.setattr("product.digest.delivery.send_notification_email",
                        lambda *a, **kw: None)
    monkeypatch.setattr(main, "_digest_in_flight", {})
    monkeypatch.setattr("product.database.SessionLocal", None)


def test_runner_flag_on_refreshes_exactly_once(monkeypatch, caplog):
    """Flag ON + non-sarah_c: across one _run_digest_for_user call,
    refresh_user_context fires exactly ONCE (the up-front call); the
    helper does NOT refresh again. The Stage 4 log line fires once.
    This is the core verification-evidence assertion for the hoist."""
    _patch_runner_pipeline(monkeypatch, owned_accounts=["Acme Logistics"])

    counter = _RefreshCounter()
    # Patch BOTH binding sites: the up-front call uses `from
    # product.context_cache import refresh_user_context as _refresh_ctx`,
    # and score_account_urgency's internal call (if it ran) would use the
    # same source-module binding. Patching the source module covers both.
    monkeypatch.setattr("product.context_cache.refresh_user_context", counter)

    # score_account_urgency is the real implementation; we want it to
    # actually receive refresh=False. The cached snapshot is set above
    # so it returns urgency for Acme based on zero CRM components.
    monkeypatch.setattr("product.signal_cache.read_signals",
                        lambda *a, **kw: [])

    captured_digest = {}
    monkeypatch.setattr(
        "product.digest.delivery.save_digest_to_disk",
        lambda d, c: captured_digest.update({"d": d, "c": c}),
    )

    with caplog.at_level(logging.INFO, logger="cs-agent"):
        with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}):
            main._run_digest_for_user("fixture_stage4_once")

    assert counter.call_count == 1, (
        f"Stage 4 hoist must produce exactly ONE refresh per digest; "
        f"got call_count={counter.call_count}. Regression to double-pull."
    )
    assert counter.csm_ids == ["fixture_stage4_once"]

    stage4_log_lines = [
        rec for rec in caplog.records
        if "Stage 4 up-front context refresh complete" in rec.message
    ]
    assert len(stage4_log_lines) == 1, (
        f"Stage 4 success log must fire exactly once; got "
        f"{len(stage4_log_lines)}"
    )

    # The digest still renders.
    assert "d" in captured_digest
    assert captured_digest["d"].get("digest_text")


def test_runner_flag_off_skips_upfront_refresh(monkeypatch):
    """Flag OFF: the up-front block is skipped entirely. Refresh_user_context
    call_count is 0 FROM THE UP-FRONT BLOCK (the legacy path may still
    refresh internally if it ran score_account_urgency, but it doesn't --
    build_morning_digest does not call score_account_urgency). The legacy
    build_morning_digest path is the one taken."""
    _patch_runner_pipeline(monkeypatch, owned_accounts=["Acme"])

    counter = _RefreshCounter()
    monkeypatch.setattr("product.context_cache.refresh_user_context", counter)

    legacy_calls = []

    def _legacy(classified, csm_name, **kw):
        legacy_calls.append((csm_name, kw))
        return {"date": "2026-06-02", "greeting": "Good morning, Test.",
                "digest_text": "LEGACY", "accounts_mentioned": [],
                "action_items": [], "meetings_today": []}

    monkeypatch.setattr("product.digest.builder.build_morning_digest", _legacy)

    monkeypatch.delenv("DIGEST_GENERATOR_ENABLED", raising=False)
    main._run_digest_for_user("fixture_flagoff_stage4")

    assert counter.call_count == 0, (
        f"Flag OFF must not run the Stage 4 up-front refresh; "
        f"got call_count={counter.call_count}"
    )
    assert len(legacy_calls) == 1, (
        "Flag OFF must take the legacy build_morning_digest path"
    )


def test_runner_sarah_c_skips_upfront_refresh_even_with_flag_on(monkeypatch):
    """sarah_c with flag ON: the up-front block is gated on the SAME
    condition as the dispatch (csm_id != 'sarah_c'), so it must NOT run.
    Demo regression guard."""
    _patch_runner_pipeline(monkeypatch, owned_accounts=["Meridian Health"])

    counter = _RefreshCounter()
    monkeypatch.setattr("product.context_cache.refresh_user_context", counter)

    legacy_calls = []
    new_calls = []

    def _legacy(classified, csm_name, **kw):
        legacy_calls.append((csm_name, kw))
        return {"date": "x", "greeting": "g", "digest_text": "SARAH_LEGACY",
                "accounts_mentioned": [], "action_items": [],
                "meetings_today": []}

    def _new(*a, **kw):
        new_calls.append(kw)
        return {"digest_text": "NEW", "date": "x", "greeting": "g",
                "accounts_mentioned": [], "action_items": [],
                "meetings_today": []}

    monkeypatch.setattr("product.digest.builder.build_morning_digest", _legacy)
    monkeypatch.setattr(main, "_build_new_digest_for_user", _new)

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}):
        main._run_digest_for_user("sarah_c")

    assert counter.call_count == 0, (
        f"sarah_c must not trigger the Stage 4 up-front refresh "
        f"(got call_count={counter.call_count}); demo regression guard"
    )
    assert len(legacy_calls) == 1
    assert len(new_calls) == 0


def test_runner_upfront_refresh_failure_falls_back_to_self_refresh(monkeypatch):
    """Failure path: if the up-front asyncio.run(refresh_user_context)
    raises, _upfront_refresh_ok stays False, score_account_urgency is
    called with refresh=True (its self-refresh runs), and the digest
    still renders. No 500. This is the no-regression guarantee."""
    _patch_runner_pipeline(monkeypatch, owned_accounts=["Acme"])

    # The up-front refresh raises; the helper's own refresh inside
    # score_account_urgency must then run successfully.
    refresh_calls = {"count": 0}

    async def _refresh(csm_id):
        refresh_calls["count"] += 1
        if refresh_calls["count"] == 1:
            raise RuntimeError("simulated up-front asyncio collision")
        return None

    monkeypatch.setattr("product.context_cache.refresh_user_context", _refresh)

    score_refresh_args = {}

    def _fake_score(csm_id, refresh=True):
        score_refresh_args["refresh"] = refresh
        return {"Acme": {"score": 0, "risk": "on_track", "drivers": []}}

    monkeypatch.setattr(
        "product.signals.detector.score_account_urgency", _fake_score,
    )

    captured_digest = {}
    monkeypatch.setattr(
        "product.digest.delivery.save_digest_to_disk",
        lambda d, c: captured_digest.update({"d": d, "c": c}),
    )

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}):
        # Must NOT raise -- the digest path must absorb the failure.
        main._run_digest_for_user("fixture_upfront_fail")

    # The up-front refresh was attempted and raised.
    assert refresh_calls["count"] == 1, (
        f"up-front refresh must have been attempted exactly once "
        f"(got {refresh_calls['count']})"
    )
    # _upfront_refresh_ok stayed False, so the helper got skip=False,
    # which means score_account_urgency was called with refresh=True.
    assert score_refresh_args.get("refresh") is True, (
        "after the up-front refresh fails, score_account_urgency must be "
        "called with refresh=True so urgency is still computed; got "
        f"refresh={score_refresh_args.get('refresh')!r}"
    )
    # The digest rendered.
    assert "d" in captured_digest
    assert captured_digest["d"].get("digest_text")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
