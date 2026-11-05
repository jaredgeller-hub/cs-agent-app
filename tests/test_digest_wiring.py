"""
tests/test_digest_wiring.py

Stage 3 integration tests for the deterministic digest generator wired
into the two live surfaces:
  - login: _run_digest_for_user calls build_digest_payload under flag
  - chat:  /chat dispatcher calls _chat_digest_intercept under flag + trigger

Every test mocks the network-touching helpers (Gmail, Calendar, CRM,
score_account_urgency, the Anthropic client) so the assembly runs as
pure-Python and the rendered markdown is fully deterministic.

These are integration tests in the sense that they exercise the full
wiring path through main._run_digest_for_user and through main.app's
/chat endpoint - not just the generator in isolation. The unit tests
for generate_digest, tier_priorities, and build_digest_payload live
in test_digest_generator.py and test_tier_priorities.py.

Standalone-runnable:
    python3 tests/test_digest_wiring.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("STRIPE_PRICE_ID", "price_test_fixture")

import pytest
from fastapi.testclient import TestClient

import main


client = TestClient(main.app)


# ── Shared helpers ──────────────────────────────────────────────────────────

def _empty_calendar(date_str: str = "2026-06-02") -> dict:
    return {
        "date": date_str,
        "customer_meetings": [],
        "internal_meetings": [],
        "total_customer_meetings": 0,
        "total_meetings": 0,
        "available_gaps": [],
    }


def _empty_week_ahead(start: str = "2026-06-02", end: str = "2026-06-08") -> dict:
    from datetime import date as _date, timedelta as _td
    by_date = {}
    cursor = _date.fromisoformat(start)
    end_d = _date.fromisoformat(end)
    while cursor <= end_d:
        by_date[cursor.isoformat()] = {"customer_meetings": [], "internal_meetings": []}
        cursor += _td(days=1)
    return {
        "start_date": start, "end_date": end,
        "timezone": "America/Chicago",
        "by_date": by_date,
        "total_customer_meetings": 0,
        "total_meetings": 0,
    }


def _populated_week_ahead(account: str) -> dict:
    wa = _empty_week_ahead()
    wa["by_date"]["2026-06-04"]["customer_meetings"].append({
        "account": account, "start": "10:00", "title": "Sync",
        "start_iso": "2026-06-04T10:00:00-05:00",
        "end": "10:30", "attendees": ["sarah@x.example"], "has_prep_block": False,
    })
    wa["total_customer_meetings"] = 1
    wa["total_meetings"] = 1
    return wa


def _patch_login_pipeline(monkeypatch, *,
                          owned_accounts: list[str],
                          urgency: dict | Exception,
                          calendar_today: dict | None = None,
                          week_ahead: dict | None = None,
                          crm_fetch_status: str = "ok"):
    """Stub every network or DB-touching helper inside _run_digest_for_user
    so the pipeline runs as pure Python.

    All helpers _run_digest_for_user uses are imported INSIDE the function
    body (e.g. `from product.org_config import get_csm_config`), so patches
    target the source module path, not `main`.

    Tests use non-`user_` csm_ids to bypass the Clerk/Postgres profile-load
    branch (main.py around the `csm_id.startswith("user_")` check).
    """
    # CSM config lookup, patched at the source module.
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
                        lambda: {"crm_type": "hubspot"})
    monkeypatch.setattr("product.org_config.get_pilot_mode", lambda: "live")
    # get_csm_timezone is a top-level def in main.py, so monkeypatching
    # via main is correct here.
    monkeypatch.setattr(main, "get_csm_timezone",
                        lambda csm_id: __import__("zoneinfo").ZoneInfo("America/Chicago"))

    # Context-cache: force the cache-miss branch so the connector code
    # path runs. crm_fetch_status determines whether we provide a working
    # connector ("ok") or simulate a connector failure ("failed").
    monkeypatch.setattr(
        "product.context_cache.get_cached_context",
        lambda csm_id: None,
    )
    if crm_fetch_status == "ok" and owned_accounts:
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
    elif crm_fetch_status == "empty":
        from product.connectors.source_status import SourceStatus as _SS
        fake_conn = MagicMock()
        fake_conn.get_accounts.return_value = []
        fake_conn.get_accounts_status.return_value = _SS.EMPTY
        monkeypatch.setattr("product.connectors.registry.get_connector",
                            lambda csm_id, cfg: fake_conn)
        monkeypatch.setattr(
            "product.signals.detector.detect_crm_signals",
            lambda *a, **kw: [],
        )
    else:
        monkeypatch.setattr("product.connectors.registry.get_connector",
                            lambda csm_id, cfg: None)
        monkeypatch.setattr(
            "product.signals.detector.detect_crm_signals",
            lambda *a, **kw: [],
        )

    # The cached-context contact fallback at main.py also reads
    # load_hubspot_token; patch it to None so the fallback is a no-op.
    monkeypatch.setattr(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        lambda csm_id: None,
    )

    # Gmail, Calendar, Drive
    monkeypatch.setattr(
        "product.signals.gmail.extract_gmail_signals",
        lambda csm_id, ac: [],
    )
    monkeypatch.setattr(
        "product.signals.calendar.extract_calendar_signals",
        lambda csm_id, names: calendar_today or _empty_calendar(),
    )
    monkeypatch.setattr(
        "product.signals.drive.find_meet_transcripts",
        AsyncMock(return_value=[]),
    )

    # score_account_urgency. Accept the Stage 4 `refresh` kwarg passed by
    # _build_new_digest_for_user; the mock ignores it and returns the canned
    # urgency dict regardless. The per-flag assertions for refresh propagation
    # live in the new Stage 4 tests, not here.
    if isinstance(urgency, Exception):
        def _raise(csm_id, **_kw):
            raise urgency
        monkeypatch.setattr(
            "product.signals.detector.score_account_urgency", _raise,
        )
    else:
        monkeypatch.setattr(
            "product.signals.detector.score_account_urgency",
            lambda csm_id, **_kw: dict(urgency),
        )

    # extract_calendar_range (week ahead)
    monkeypatch.setattr(
        "product.signals.calendar.extract_calendar_range",
        lambda csm_id, names, s, e: week_ahead or _empty_week_ahead(),
    )

    # Voice slots - canned, no LLM call.
    monkeypatch.setattr(
        "product.digest.generator._generate_voice_slots",
        lambda csm_name, tier_a, tier_b, crm_type: {
            "priority_line": "PRIORITY_LINE",
            "closing_line": "CLOSING_LINE",
        },
    )

    # Suppress downstream side effects.
    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk",
                        lambda *a, **kw: None)
    monkeypatch.setattr("product.digest.delivery.send_notification_email",
                        lambda *a, **kw: None)
    monkeypatch.setattr(main, "_digest_in_flight", {})
    # DB save - no-op (DigestLog save is wrapped in its own try/except so
    # SessionLocal=None just logs a warning and the digest still returns).
    monkeypatch.setattr("product.database.SessionLocal", None)


# ── Login wiring tests ──────────────────────────────────────────────────────

def test_login_flag_off_uses_legacy_build_morning_digest(monkeypatch):
    """With DIGEST_GENERATOR_ENABLED unset, _run_digest_for_user calls
    build_morning_digest unchanged."""
    _patch_login_pipeline(
        monkeypatch,
        owned_accounts=["Acme"],
        urgency={"Acme": {"score": 70, "risk": "at_risk", "drivers": ["x"]}},
    )

    legacy_calls = []
    new_calls = []

    def _legacy(classified, csm_name, **kw):
        legacy_calls.append((csm_name, kw))
        return {
            "date": "2026-06-02", "greeting": "Good morning, Test.",
            "digest_text": "LEGACY_DIGEST",
            "accounts_mentioned": [], "action_items": [], "meetings_today": [],
        }

    def _new_wrapper_sentinel(*a, **kw):
        new_calls.append(kw)
        return {"digest_text": "NEW_DIGEST", "date": "x", "greeting": "g",
                "accounts_mentioned": [], "action_items": [], "meetings_today": []}

    monkeypatch.setattr("product.digest.builder.build_morning_digest", _legacy)
    monkeypatch.setattr(main, "_build_new_digest_for_user", _new_wrapper_sentinel)

    monkeypatch.delenv("DIGEST_GENERATOR_ENABLED", raising=False)
    main._run_digest_for_user("fixture_flagoff")

    assert len(legacy_calls) == 1
    assert len(new_calls) == 0


def test_login_flag_on_uses_new_generator(monkeypatch):
    """With DIGEST_GENERATOR_ENABLED=true, _run_digest_for_user calls the
    new build_digest_payload-shaped wrapper instead of build_morning_digest."""
    _patch_login_pipeline(
        monkeypatch,
        owned_accounts=["Acme Logistics"],
        urgency={"Acme Logistics": {"score": 70, "risk": "at_risk",
                                    "drivers": ["No email reply in 12 days"]}},
    )

    legacy_calls = []

    def _legacy(*a, **kw):
        legacy_calls.append(kw)
        return {"date": "x", "greeting": "g", "digest_text": "LEGACY",
                "accounts_mentioned": [], "action_items": [], "meetings_today": []}

    monkeypatch.setattr("product.digest.builder.build_morning_digest", _legacy)

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}):
        main._run_digest_for_user("fixture_flagon")

    # Legacy path must NOT have been called.
    assert len(legacy_calls) == 0


def test_login_flag_on_renders_new_generator_skeleton(monkeypatch, capsys):
    """End-to-end check: flag-on digest contains the deterministic
    skeleton markers, the urgency-driven account line, and the voice
    slots. Captures the digest_text for visual inspection."""
    captured = {}

    _patch_login_pipeline(
        monkeypatch,
        owned_accounts=["Acme Logistics", "Beacon Financial"],
        urgency={
            "Acme Logistics": {"score": 70, "risk": "at_risk",
                               "drivers": ["No email reply in 12 days",
                                           "Renewal in 47 days"]},
            "Beacon Financial": {"score": 35, "risk": "watch",
                                 "drivers": ["Renewal in 45 days"]},
        },
        week_ahead=_populated_week_ahead("Acme Logistics"),
    )

    _orig = main.build_digest_payload if hasattr(main, "build_digest_payload") else None

    def _capture_save(digest, csm_id):
        captured["digest"] = digest

    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk", _capture_save)

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}):
        main._run_digest_for_user("fixture_skel")

    digest = captured["digest"]
    text = digest["digest_text"]
    print("\n\n========== FLAG-ON LOGIN DIGEST ==========\n")
    print(text)
    print("\n========== END FLAG-ON LOGIN DIGEST ==========\n")
    assert "### Urgent today" in text
    assert "### Proactive this week" in text
    assert "### The week ahead" in text
    assert "Acme Logistics" in text
    assert "Beacon Financial" in text
    # Voice slots present and verbatim from the mock.
    assert "PRIORITY_LINE" in text
    assert "CLOSING_LINE" in text


def test_login_score_account_urgency_failure_returns_rendered_digest(monkeypatch):
    """If score_account_urgency raises, the digest renders with urgency={}
    (degraded). The endpoint does NOT 500."""
    captured = {}
    _patch_login_pipeline(
        monkeypatch,
        owned_accounts=["Acme"],
        urgency=RuntimeError("simulated refresh failure"),
        week_ahead=_populated_week_ahead("Acme"),
    )

    def _capture(d, c):
        captured["digest"] = d
    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk", _capture)
    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk", _capture)

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}):
        # Must not raise.
        main._run_digest_for_user("fixture_urgfail")

    text = captured["digest"]["digest_text"]
    # Skeleton renders; the week-ahead floor is present.
    assert "### The week ahead" in text
    # No tier-A / tier-B account lines (urgency was {}, and the calendar
    # mock returned no meetings today so meeting-promotion is also empty).
    assert "score 70" not in text
    assert "score 35" not in text


def test_login_empty_owned_accounts_renders_honest_empty(monkeypatch):
    """An owner with no accounts: honest-empty digest text reached through
    the full wired pipeline."""
    captured = {}
    _patch_login_pipeline(
        monkeypatch,
        owned_accounts=[],
        urgency={},
        crm_fetch_status="empty",
    )

    def _capture(d, c):
        captured["digest"] = d
    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk", _capture)
    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk", _capture)

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}):
        main._run_digest_for_user("fixture_emptyowned")

    text = captured["digest"]["digest_text"]
    assert "No accounts are assigned to you" in text
    assert captured["digest"]["accounts_mentioned"] == []


def test_login_urgency_for_non_owned_account_is_filtered_leak_guard(monkeypatch):
    """Leak-class regression: a stale urgency entry for an account NOT in
    the current owned set must NOT appear in the rendered digest.
    Closes the residual leak surfaced by the Stage 3A gate question."""
    captured = {}
    _patch_login_pipeline(
        monkeypatch,
        owned_accounts=["Owned Co"],
        urgency={
            "Owned Co":    {"score": 70, "risk": "at_risk", "drivers": ["x"]},
            "Stale Co":    {"score": 85, "risk": "at_risk", "drivers": ["y"]},
            "Not Mine Co": {"score": 60, "risk": "at_risk", "drivers": ["z"]},
        },
        week_ahead=_populated_week_ahead("Owned Co"),
    )

    def _capture(d, c):
        captured["digest"] = d
    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk", _capture)
    monkeypatch.setattr("product.digest.delivery.save_digest_to_disk", _capture)

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}):
        main._run_digest_for_user("fixture_leakguard")

    text = captured["digest"]["digest_text"]
    assert "Owned Co" in text
    assert "Stale Co" not in text
    assert "Not Mine Co" not in text
    # The accounts_mentioned list cannot contain leaked names either.
    for a in captured["digest"]["accounts_mentioned"]:
        assert a == "Owned Co", f"leaked account in accounts_mentioned: {a}"


def test_login_sarah_c_never_invokes_new_generator(monkeypatch):
    """sarah_c is the regression guard. Even with the flag ON, her path
    uses build_morning_digest, not the new generator."""
    _patch_login_pipeline(
        monkeypatch,
        owned_accounts=["Meridian Health"],
        urgency={"Meridian Health": {"score": 70, "risk": "at_risk", "drivers": ["x"]}},
    )

    legacy_calls = []
    new_calls = []

    def _legacy(classified, csm_name, **kw):
        legacy_calls.append((csm_name, kw))
        return {"date": "2026-06-02", "greeting": "Good morning, Sarah.",
                "digest_text": "SARAH_LEGACY_DIGEST",
                "accounts_mentioned": [], "action_items": [], "meetings_today": []}

    def _new(*a, **kw):
        new_calls.append(kw)
        return {"digest_text": "NEW_DIGEST", "date": "x", "greeting": "g",
                "accounts_mentioned": [], "action_items": [], "meetings_today": []}

    monkeypatch.setattr("product.digest.builder.build_morning_digest", _legacy)
    monkeypatch.setattr(main, "_build_new_digest_for_user", _new)

    # Sarah's path has its own get_csm_config + store.json branches. We
    # only need to make sure her csm_id branches around the flag check.
    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}):
        main._run_digest_for_user("sarah_c")

    assert len(legacy_calls) == 1, "sarah_c must hit build_morning_digest exactly once"
    assert len(new_calls) == 0, "sarah_c must NEVER hit the new generator wrapper"


# ── Chat-intercept tests (through the /chat endpoint) ──────────────────────

def _fake_session(csm_id: str = "user_chat_test", name: str = "Test User") -> dict:
    return {"csm_id": csm_id, "name": name, "email": "test@example.com"}


def _trialing_session_factory() -> MagicMock:
    fake_row = MagicMock()
    fake_row.subscription_status = "trialing"
    fake_session = MagicMock()
    fake_session.query.return_value.filter_by.return_value.first.return_value = fake_row
    return MagicMock(return_value=fake_session)


def test_chat_digest_request_intercepted_and_returns_generator_output(monkeypatch):
    """A clear digest ask returns the deterministic generator output verbatim,
    bypassing the tool-use orchestration entirely."""
    # Patch the intercept-path data fetchers and voice slots.
    monkeypatch.setattr("product.chat_tools.registry._load_account_names_for_csm",
                        lambda csm_id: ["Acme Logistics"])
    monkeypatch.setattr("product.signals.calendar.extract_calendar_signals",
                        lambda csm_id, names: _empty_calendar())
    monkeypatch.setattr("product.signals.calendar.extract_calendar_range",
                        lambda csm_id, names, s, e: _populated_week_ahead("Acme Logistics"))
    monkeypatch.setattr("product.signals.detector.score_account_urgency",
                        lambda csm_id, **_kw: {
                            "Acme Logistics": {
                                "score": 70, "risk": "at_risk",
                                "drivers": ["No email reply in 12 days"],
                            },
                        })
    monkeypatch.setattr("product.context_cache.get_cached_context",
                        lambda csm_id: {"data": {"accounts": [{"name": "Acme Logistics"}],
                                                 "crm_type": "hubspot"},
                                         "refreshed_at": None, "refresh_status": "complete",
                                         "source_status": {}})
    monkeypatch.setattr("product.digest.generator._generate_voice_slots",
                        lambda csm_name, tier_a, tier_b, crm_type: {
                            "priority_line": "PRIORITY_LINE",
                            "closing_line": "CLOSING_LINE",
                        })

    # The fake anthropic client below would be hit if the intercept didn't
    # match; if a test invokes it, the test fails its own assertion.
    fake_client = MagicMock(name="anthropic_client")
    fake_client.messages.create.side_effect = AssertionError(
        "tool-use LLM was invoked - digest intercept should have bypassed it"
    )

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true",
                                   "CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client):
        resp = client.post("/chat", json={"message": "my digest"})

    assert resp.status_code == 200
    data = resp.json()
    assert "Acme Logistics" in data["reply"]
    assert "### Urgent today" in data["reply"]
    assert "### The week ahead" in data["reply"]
    assert "PRIORITY_LINE" in data["reply"]
    # drafts list present and empty.
    assert data.get("drafts") == []
    # Tool-use client never invoked.
    assert fake_client.messages.create.call_count == 0


def test_chat_normal_question_is_not_intercepted(monkeypatch):
    """A non-digest question flows to the normal tool-use chat handler."""
    monkeypatch.setattr("product.chat_tools.registry._load_account_names_for_csm",
                        lambda csm_id: ["Acme Logistics"])

    # Tool-use mock: one end_turn response is enough.
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "Normal answer."
    fake_resp = MagicMock()
    fake_resp.content = [fake_block]
    fake_resp.stop_reason = "end_turn"
    fake_resp.usage = MagicMock(
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
        input_tokens=10, output_tokens=5,
    )
    fake_client = MagicMock(name="anthropic_client")
    fake_client.messages.create.return_value = fake_resp

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true",
                                   "CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client):
        resp = client.post("/chat", json={"message": "what does Northgate say about renewal?"})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "Normal answer."
    # Confirms tool-use path was reached.
    assert fake_client.messages.create.call_count == 1


def test_chat_intercept_off_when_flag_off(monkeypatch):
    """With DIGEST_GENERATOR_ENABLED unset, a digest ask flows to normal
    tool-use; the intercept is not engaged."""
    monkeypatch.setattr("product.chat_tools.registry._load_account_names_for_csm",
                        lambda csm_id: ["Acme Logistics"])

    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "Tool-use answer to digest ask."
    fake_resp = MagicMock()
    fake_resp.content = [fake_block]
    fake_resp.stop_reason = "end_turn"
    fake_resp.usage = MagicMock(
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
        input_tokens=10, output_tokens=5,
    )
    fake_client = MagicMock(name="anthropic_client")
    fake_client.messages.create.return_value = fake_resp

    monkeypatch.delenv("DIGEST_GENERATOR_ENABLED", raising=False)
    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client):
        resp = client.post("/chat", json={"message": "my digest"})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "Tool-use answer to digest ask."
    assert fake_client.messages.create.call_count == 1


def test_chat_sarah_c_never_intercepted_even_with_flag_on(monkeypatch):
    """sarah_c digest ask with the flag ON still flows to the demo legacy
    chat path. The new generator must never run for her."""
    # Anthropic client mock for the legacy single-shot path.
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "Sarah legacy demo reply."
    fake_resp = MagicMock()
    fake_resp.content = [fake_block]
    fake_resp.stop_reason = "end_turn"
    fake_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    fake_client = MagicMock(name="anthropic_client")
    fake_client.messages.create.return_value = fake_resp

    # Patch the new-generator wrapper to fail the test if it's invoked.
    def _new_wrapper(*a, **kw):
        raise AssertionError(
            "new generator wrapper was called for sarah_c - regression!"
        )
    monkeypatch.setattr(main, "_build_new_digest_for_user", _new_wrapper)
    monkeypatch.setattr(main, "_chat_digest_intercept",
                        AsyncMock(side_effect=AssertionError(
                            "chat digest intercept was called for sarah_c - regression!"
                        )))

    with patch.dict(os.environ, {"DIGEST_GENERATOR_ENABLED": "true",
                                   "CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "_anthropic_client", fake_client):
        resp = client.post("/chat", json={"message": "my digest"})

    # _resolve_auth returns None -> _chat_csm_id falls back to "sarah_c"
    # per the dispatcher logic. Status 200 with the legacy reply.
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Sarah legacy demo reply."


# ── Trigger condition unit tests ────────────────────────────────────────────

def test_is_digest_request_recognizes_canonical_phrases():
    for phrase in [
        "/digest", "my digest", "morning digest", "show me my day",
        "what's my day", "give me my digest",
    ]:
        assert main._is_digest_request(phrase), f"should match: {phrase!r}"


def test_is_digest_request_handles_punctuation_and_case():
    assert main._is_digest_request("My Digest.")
    assert main._is_digest_request("WHAT'S MY DAY?")
    assert main._is_digest_request("  my digest  ")


def test_is_digest_request_does_not_match_normal_questions():
    for phrase in [
        "what's on my plate today?",
        "show me Northgate's digest",
        "what does today's digest say about Northgate?",
        "send a digest email",
        "compile a digest of recent activity",
        "what should I do today",
        "northgate update",
        "",
    ]:
        assert not main._is_digest_request(phrase), f"should NOT match: {phrase!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
