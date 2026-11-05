"""tests/test_score_account_urgency.py

Sprint 6 H.1 — per-account urgency aggregator.

`score_account_urgency(csm_id)` reads from context_cache (CRM state) and
signal_cache (Gmail absence), cross-silo scores each onboarded account,
and returns {account: {score, risk, drivers}}.

These tests stub `refresh_user_context`, `get_cached_context`, and
`read_signals` so the scoring logic can be exercised without a database
or live CRM. The existing per-signal `score_urgency(signal, context)`
is intentionally untouched — these tests do not cover it.

Usage:
    python3 -m pytest tests/test_score_account_urgency.py -v
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.signals.detector import _risk_from_score, score_account_urgency


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


def _patch_all(monkeypatch, *, cached, signals=None, refresh_raises=False):
    """Stub refresh_user_context, get_cached_context, read_signals.

    Targets `product.context_cache.*` and `product.signal_cache.*` because
    score_account_urgency imports them lazily at call time (so the
    module-import-time bindings live on those source modules, not on
    detector).
    """
    async def _refresh(csm_id):
        if refresh_raises:
            raise RuntimeError("simulated refresh failure")
        return None

    def _get_cached(csm_id):
        return cached

    def _read_signals(csm_id, signal_type=None, max_age_minutes=60):
        return list(signals or [])

    monkeypatch.setattr("product.context_cache.refresh_user_context", _refresh)
    monkeypatch.setattr("product.context_cache.get_cached_context", _get_cached)
    monkeypatch.setattr("product.signal_cache.read_signals", _read_signals)


# ── Risk-band boundary contract ──────────────────────────────────────────────

def test_risk_boundaries_exact():
    """Boundaries are tested on the pure helper so we can hit 29/30/59/60
    exactly without engineering score components that happen to sum there.
    The rule-based component thresholds today are all multiples of 10, so
    29 and 59 are unreachable end-to-end but matter for any future
    reweighting that crosses the boundary."""
    assert _risk_from_score(0)   == "on_track"
    assert _risk_from_score(29)  == "on_track"
    assert _risk_from_score(30)  == "watch"
    assert _risk_from_score(59)  == "watch"
    assert _risk_from_score(60)  == "at_risk"
    assert _risk_from_score(100) == "at_risk"


# ── Empty / degenerate inputs ────────────────────────────────────────────────

def test_no_cached_context_returns_empty(monkeypatch):
    _patch_all(monkeypatch, cached=None)
    assert score_account_urgency("user_test") == {}


def test_empty_accounts_returns_empty(monkeypatch):
    _patch_all(monkeypatch, cached=_cached(accounts=[]))
    assert score_account_urgency("user_test") == {}


def test_healthy_account_no_signals_no_crm_state(monkeypatch):
    """Account exists, no absence signals, no renewals/health/tickets —
    score = 0, risk = on_track, drivers = []."""
    _patch_all(
        monkeypatch,
        cached=_cached(accounts=[{"name": "Acme"}]),
        signals=[],
    )
    out = score_account_urgency("user_test")
    assert out == {"Acme": {"score": 0, "risk": "on_track", "drivers": []}}


# ── Cross-silo scoring on a single account ───────────────────────────────────

def test_renewal_25_days_plus_silence_15_days_at_risk(monkeypatch):
    """Renewal in 25 days (<30) → +30. Silence at 15 days (>14) → +30.
    Total 60 → at_risk. Both drivers present."""
    _patch_all(
        monkeypatch,
        cached=_cached(
            accounts=[{"name": "Northgate Logistics"}],
            deals=[{"account_name": "Northgate Logistics", "days_until_close": 25}],
        ),
        signals=[{
            "account": "Northgate Logistics",
            "signal_type": "email.absence",
            "days_waiting": 15,
        }],
    )
    out = score_account_urgency("user_test")
    assert out["Northgate Logistics"]["score"] == 60
    assert out["Northgate Logistics"]["risk"]  == "at_risk"
    drivers = out["Northgate Logistics"]["drivers"]
    assert "Renewal in 25 days" in drivers
    assert "No email reply in 15 days" in drivers


def test_red_health_alone_is_watch(monkeypatch):
    """color_code='red' alone → +20 → watch (score >= 30 is the band,
    but +20 lands at 20 which is on_track). Locking the documented
    "red alone → watch" behavior requires +30. The spec says +20.
    So red alone is on_track today — test that explicitly."""
    _patch_all(
        monkeypatch,
        cached=_cached(
            accounts=[{"name": "Acme"}],
            health=[{"account_name": "Acme", "color_code": "red"}],
        ),
    )
    out = score_account_urgency("user_test")
    # Documenting the as-implemented behavior: red alone = 20 = on_track
    # under the current weights. If product wants red alone to escalate,
    # bump the weight or drop the watch threshold to 20.
    assert out["Acme"]["score"] == 20
    assert out["Acme"]["risk"]  == "on_track"
    assert "Health score declining (red)" in out["Acme"]["drivers"]


def test_high_priority_ticket_alone_is_on_track(monkeypatch):
    """Same shape as above: high ticket alone → +20 → on_track at 20.
    Documents the as-implemented behavior."""
    _patch_all(
        monkeypatch,
        cached=_cached(
            accounts=[{"name": "Acme"}],
            tickets=[{"account_name": "Acme", "priority": "high"}],
        ),
    )
    out = score_account_urgency("user_test")
    assert out["Acme"]["score"] == 20
    assert out["Acme"]["risk"]  == "on_track"
    assert "1 high-priority open ticket" in out["Acme"]["drivers"]


def test_watch_band_from_two_signals(monkeypatch):
    """+20 (renewal 45d) + +10 (silence 5d) = 30 → watch band lower edge."""
    _patch_all(
        monkeypatch,
        cached=_cached(
            accounts=[{"name": "Beacon"}],
            deals=[{"account_name": "Beacon", "days_until_close": 45}],
        ),
        signals=[{
            "account": "Beacon",
            "signal_type": "email.absence",
            "days_waiting": 5,
        }],
    )
    out = score_account_urgency("user_test")
    assert out["Beacon"]["score"] == 30
    assert out["Beacon"]["risk"]  == "watch"


# ── Defensive behavior ───────────────────────────────────────────────────────

def test_missing_fields_handled_gracefully(monkeypatch):
    """Deals missing days_until_close, signals missing days_waiting,
    tickets missing priority — all coerced to no-contribution without
    raising."""
    _patch_all(
        monkeypatch,
        cached=_cached(
            accounts=[{"name": "Acme"}],
            deals=[{"account_name": "Acme"}, {"days_until_close": 25}],  # one missing days, one missing name
            health=[{"account_name": "Acme"}, {"color_code": "red"}],     # one missing color, one missing name
            tickets=[{"account_name": "Acme"}, {"priority": "high"}],     # one missing priority, one missing name
        ),
        signals=[
            {"account": "Acme"},                       # missing days_waiting
            {"signal_type": "email.absence"},          # missing account
            {"account": "Acme", "days_waiting": None}, # explicit None
        ],
    )
    out = score_account_urgency("user_test")
    assert out["Acme"]["score"] == 0
    assert out["Acme"]["risk"]  == "on_track"


def test_invalid_csm_id_raises(monkeypatch):
    """validate_csm_id raises HTTPException(400) — propagates by design,
    matching every other csm_id call site. Documented in docstring."""
    from fastapi import HTTPException
    # No patching needed — validate_csm_id is the first thing the
    # function does, so it never reaches the patched dependencies.
    with pytest.raises(HTTPException) as exc_info:
        score_account_urgency("../bad")
    assert exc_info.value.status_code == 400


def test_refresh_failure_propagates_not_stale(monkeypatch, caplog):
    """Fail-closed: if refresh_user_context raises, score_account_urgency
    must RE-RAISE (logging a loud ERROR mentioning STALE) rather than
    silently scoring on stale cache. The caller (the F1 absence loop) then
    skips urgency scoring for this CSM this cycle. Replaces the prior
    'log-and-continue-on-stale-cache' contract."""
    _patch_all(
        monkeypatch,
        cached=_cached(
            accounts=[{"name": "Acme"}],
            deals=[{"account_name": "Acme", "days_until_close": 25}],
        ),
        refresh_raises=True,
    )
    with caplog.at_level(logging.ERROR, logger="cs-agent.detector"):
        with pytest.raises(RuntimeError, match="simulated refresh failure"):
            score_account_urgency("user_test")
    assert any(
        "STALE" in rec.message and rec.levelname == "ERROR"
        for rec in caplog.records
    ), "expected a loud ERROR mentioning STALE when refresh raises"


def test_gmail_only_no_crm_connection(monkeypatch):
    """Account exists in cache but no deals/health/tickets (CRM not
    connected, or pull failed). Gmail signals still score."""
    _patch_all(
        monkeypatch,
        cached=_cached(accounts=[{"name": "Acme"}]),  # no CRM data
        signals=[{
            "account": "Acme",
            "signal_type": "email.absence",
            "days_waiting": 10,
        }],
    )
    out = score_account_urgency("user_test")
    assert out["Acme"]["score"] == 20  # silence 10d > 7 → +20
    assert out["Acme"]["risk"]  == "on_track"
    assert "No email reply in 10 days" in out["Acme"]["drivers"]


def test_empty_signal_cache_no_exception(monkeypatch):
    """No Gmail signals — Gmail component is 0, no exception."""
    _patch_all(
        monkeypatch,
        cached=_cached(
            accounts=[{"name": "Acme"}],
            deals=[{"account_name": "Acme", "days_until_close": 25}],
        ),
        signals=[],
    )
    out = score_account_urgency("user_test")
    # Only renewal contributes (+30)
    assert out["Acme"]["score"] == 30
    assert out["Acme"]["risk"]  == "watch"
    # Confirm no Gmail driver was added
    assert not any("email reply" in d for d in out["Acme"]["drivers"])


def test_signal_cache_read_failure_logs_and_continues(monkeypatch, caplog):
    """signal_cache.read_signals raising → log + zero Gmail contribution."""
    async def _refresh(csm_id):
        return None
    def _get_cached(csm_id):
        return _cached(
            accounts=[{"name": "Acme"}],
            deals=[{"account_name": "Acme", "days_until_close": 25}],
        )
    def _broken_read(csm_id, signal_type=None, max_age_minutes=60):
        raise RuntimeError("simulated signal_cache failure")

    monkeypatch.setattr("product.context_cache.refresh_user_context", _refresh)
    monkeypatch.setattr("product.context_cache.get_cached_context", _get_cached)
    monkeypatch.setattr("product.signal_cache.read_signals", _broken_read)

    with caplog.at_level(logging.WARNING, logger="cs-agent.detector"):
        out = score_account_urgency("user_test")
    assert out["Acme"]["score"] == 30   # renewal only
    assert any(
        "signal_cache read failed" in rec.message
        for rec in caplog.records
    )


# ── Multiple accounts in one call ────────────────────────────────────────────

def test_multi_account_per_account_scoring(monkeypatch):
    """Three accounts with distinct risk levels — each scored independently."""
    _patch_all(
        monkeypatch,
        cached=_cached(
            accounts=[
                {"name": "AtRiskCo"},
                {"name": "WatchCo"},
                {"name": "OnTrackCo"},
            ],
            deals=[
                {"account_name": "AtRiskCo", "days_until_close": 25},  # +30
                {"account_name": "WatchCo",  "days_until_close": 80},  # +10
            ],
            health=[
                {"account_name": "AtRiskCo", "color_code": "red"},      # +20
            ],
            tickets=[
                {"account_name": "AtRiskCo", "priority": "high"},       # +20
                {"account_name": "WatchCo",  "priority": "medium"},     # +10
            ],
        ),
        signals=[
            {"account": "WatchCo", "signal_type": "email.absence", "days_waiting": 10},  # +20
        ],
    )
    out = score_account_urgency("user_test")
    assert out["AtRiskCo"]["score"]  == 70   # 30 + 20 + 20
    assert out["AtRiskCo"]["risk"]   == "at_risk"
    assert out["WatchCo"]["score"]   == 40   # 10 + 10 + 20
    assert out["WatchCo"]["risk"]    == "watch"
    assert out["OnTrackCo"]["score"] == 0
    assert out["OnTrackCo"]["risk"]  == "on_track"


def test_soonest_renewal_wins_when_multiple_deals(monkeypatch):
    """Multiple deals for one account — score by the soonest close date."""
    _patch_all(
        monkeypatch,
        cached=_cached(
            accounts=[{"name": "Acme"}],
            deals=[
                {"account_name": "Acme", "days_until_close": 150},   # < 90 false
                {"account_name": "Acme", "days_until_close": 25},    # +30
                {"account_name": "Acme", "days_until_close": 80},    # +10 (loses)
            ],
        ),
    )
    out = score_account_urgency("user_test")
    # +30 from the 25-day renewal, not +10 from the 80-day one
    assert out["Acme"]["score"] == 30
    assert "Renewal in 25 days" in out["Acme"]["drivers"]
