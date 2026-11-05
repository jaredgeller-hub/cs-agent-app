"""
tests/test_detect_crm_signals_scoping.py

Owner-scoping invariant for the digest's CRM signal extractor.

Background: detect_crm_signals(connector) historically returned every signal
the connector surfaced. The HubSpot connector's get_renewals / get_open_ctas
are portfolio-wide MCP queries (search_crm over deals/tickets, no owner filter
applied at the MCP layer). Before this change, a CSM who owned zero accounts
could still receive a digest naming another CSM's renewals and CTAs. That is
the same confidentiality leak as F1-M, on a different code path.

Fix: detect_crm_signals accepts an optional allowed_accounts: set[str]. When
provided, signals whose account name is not in the set are dropped before the
function returns. Empty set yields zero signals (fail-closed). None preserves
the historical behavior so non-digest callers are not perturbed.

Tests cover the four cases stipulated in the Stage 2 brief:
  (i)   allowed_accounts=None preserves current behavior,
  (ii)  empty set yields zero CRM signals,
  (iii) a subset keeps only that subset,
  (iv)  unowned accounts drop across all three signal types,
across both a HubSpot-shaped and a Gainsight-shaped fake connector. Plus an
exact-match guard so a future regression to one-sided lowercasing fails loudly,
and trace-log assertions so the verification log line ("owned=N signals_in=N
signals_out=N") cannot be silently removed.
"""

import logging
from datetime import date, timedelta

from product.signals.detector import detect_crm_signals


# ── Fake connectors ──────────────────────────────────────────────────────────

class _FakeConnector:
    """
    Minimal connector surface: only the three methods detect_crm_signals reads.
    HubSpot and Gainsight emit the same dict shape (account_name as the display
    string after any post-resolution); the only difference in practice is what
    account names show up. We use one class for both shapes and vary the data.
    """

    def __init__(self, renewals=None, health=None, ctas=None):
        self._renewals = renewals or []
        self._health = health or []
        self._ctas = ctas or []

    def get_renewals(self):
        return list(self._renewals)

    def get_health_scores(self):
        return list(self._health)

    def get_open_ctas(self):
        return list(self._ctas)


def _yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def _hubspot_inputs():
    """Three accounts with at least one signal of each type apiece."""
    renewals = [
        {"account_name": "Acme",  "days_until_renewal": 20, "renewal_date": "2026-06-21", "contract_value": 100000},
        {"account_name": "Beta",  "days_until_renewal": 55, "renewal_date": "2026-07-26", "contract_value":  50000},
        {"account_name": "Gamma", "days_until_renewal": 80, "renewal_date": "2026-08-20", "contract_value":  75000},
    ]
    health = [
        {"account_name": "Acme",  "score": 50, "trend": "declining"},   # score<70 -> health_score_low
        {"account_name": "Beta",  "score": 65, "trend": "stable"},      # score<70 -> health_score_low
        {"account_name": "Gamma", "score": 90, "trend": "declining"},   # score>=70 but declining -> health_declining
    ]
    ctas = [
        {"account_name": "Acme",  "title": "Renewal call",    "due_date": _yesterday_iso(), "owner": "alice"},
        {"account_name": "Beta",  "title": "QBR prep",        "due_date": _yesterday_iso(), "owner": "bob"},
        {"account_name": "Gamma", "title": "Health check-in", "due_date": _yesterday_iso(), "owner": "carol"},
    ]
    return renewals, health, ctas


def _gainsight_inputs():
    """Different account names, same dict shape (Gainsight's _get_*_async also
    emit account_name in the result dicts; see product/connectors/gainsight.py)."""
    renewals = [
        {"account_name": "Northgate Logistics", "days_until_renewal": 14, "renewal_date": "2026-06-15", "contract_value": 120000},
        {"account_name": "Meridian Health",     "days_until_renewal": 60, "renewal_date": "2026-07-31", "contract_value":  95000},
    ]
    health = [
        {"account_name": "Northgate Logistics", "score": 40, "trend": "declining"},
        {"account_name": "Sutton Retail",       "score": 90, "trend": "stable"},
    ]
    ctas = [
        {"account_name": "Northgate Logistics", "title": "Risk review",       "due_date": _yesterday_iso(), "owner": "x"},
        {"account_name": "Sutton Retail",       "title": "Onboarding step 4", "due_date": _yesterday_iso(), "owner": "y"},
    ]
    return renewals, health, ctas


# ── (i) allowed_accounts=None preserves current behavior ─────────────────────

def test_allowed_accounts_none_hubspot_preserves_behavior():
    renewals, health, ctas = _hubspot_inputs()
    conn = _FakeConnector(renewals=renewals, health=health, ctas=ctas)
    signals = detect_crm_signals(conn)  # no allowed_accounts kwarg

    accounts = {s["account"] for s in signals}
    types = {s["type"] for s in signals}

    assert accounts == {"Acme", "Beta", "Gamma"}
    assert "crm.renewal_proximity" in types
    assert "crm.health_score_low" in types       # Acme 50, Beta 65
    assert "crm.health_declining" in types        # Gamma stable-but-declining-trend
    assert "crm.cta_overdue" in types


def test_allowed_accounts_none_gainsight_preserves_behavior():
    renewals, health, ctas = _gainsight_inputs()
    conn = _FakeConnector(renewals=renewals, health=health, ctas=ctas)
    signals = detect_crm_signals(conn)
    accounts = {s["account"] for s in signals}
    assert accounts == {"Northgate Logistics", "Meridian Health", "Sutton Retail"}


# ── (ii) Empty set yields zero CRM signals ───────────────────────────────────

def test_allowed_accounts_empty_set_yields_zero_signals_hubspot():
    """Fail-closed: zero-owner CSM gets zero CRM signals, never portfolio-wide."""
    renewals, health, ctas = _hubspot_inputs()
    conn = _FakeConnector(renewals=renewals, health=health, ctas=ctas)
    signals = detect_crm_signals(conn, allowed_accounts=set())
    assert signals == []


def test_allowed_accounts_empty_set_yields_zero_signals_gainsight():
    renewals, health, ctas = _gainsight_inputs()
    conn = _FakeConnector(renewals=renewals, health=health, ctas=ctas)
    signals = detect_crm_signals(conn, allowed_accounts=set())
    assert signals == []


# ── (iii) Subset keeps only that subset ──────────────────────────────────────

def test_allowed_accounts_subset_keeps_only_subset_hubspot():
    renewals, health, ctas = _hubspot_inputs()
    conn = _FakeConnector(renewals=renewals, health=health, ctas=ctas)
    signals = detect_crm_signals(conn, allowed_accounts={"Acme"})

    accounts = {s["account"] for s in signals}
    assert accounts == {"Acme"}

    # All three signal types for Acme survived the filter (renewal, health_score_low, cta_overdue).
    types = {s["type"] for s in signals}
    assert types == {"crm.renewal_proximity", "crm.health_score_low", "crm.cta_overdue"}


def test_allowed_accounts_subset_keeps_only_subset_gainsight():
    renewals, health, ctas = _gainsight_inputs()
    conn = _FakeConnector(renewals=renewals, health=health, ctas=ctas)
    signals = detect_crm_signals(conn, allowed_accounts={"Northgate Logistics"})
    accounts = {s["account"] for s in signals}
    assert accounts == {"Northgate Logistics"}


# ── (iv) Unowned accounts drop across all three signal types ─────────────────

def test_unowned_account_dropped_across_all_three_signal_types():
    """OwnedCo has one renewal + one health_score_low + one CTA; OtherCo has
    the same. With allowed_accounts={'OwnedCo'}, no OtherCo signal of any
    type should survive."""
    conn = _FakeConnector(
        renewals=[
            {"account_name": "OwnedCo", "days_until_renewal": 30, "renewal_date": "2026-07-01", "contract_value": 1},
            {"account_name": "OtherCo", "days_until_renewal": 30, "renewal_date": "2026-07-01", "contract_value": 1},
        ],
        health=[
            {"account_name": "OwnedCo", "score": 50, "trend": "stable"},
            {"account_name": "OtherCo", "score": 50, "trend": "stable"},
        ],
        ctas=[
            {"account_name": "OwnedCo", "title": "X", "due_date": _yesterday_iso(), "owner": "a"},
            {"account_name": "OtherCo", "title": "Y", "due_date": _yesterday_iso(), "owner": "b"},
        ],
    )
    signals = detect_crm_signals(conn, allowed_accounts={"OwnedCo"})

    assert {s["account"] for s in signals} == {"OwnedCo"}
    assert all(s["account"] != "OtherCo" for s in signals)
    # And every signal type the inputs could produce is represented for OwnedCo,
    # so a stray "filter applied only to renewals" regression would surface here.
    assert {s["type"] for s in signals} == {
        "crm.renewal_proximity",
        "crm.health_score_low",
        "crm.cta_overdue",
    }


# ── Exact-string match (no one-sided lowercasing) ────────────────────────────

def test_exact_string_match_no_one_sided_lowercasing():
    """Case-sensitive equality. If someone "normalizes" one side later, the
    'ACME' renewal will leak when only 'Acme' is owned, which this catches."""
    conn = _FakeConnector(
        renewals=[
            {"account_name": "Acme", "days_until_renewal": 30, "renewal_date": "2026-07-01", "contract_value": 1},
            {"account_name": "ACME", "days_until_renewal": 30, "renewal_date": "2026-07-01", "contract_value": 1},
            {"account_name": "acme", "days_until_renewal": 30, "renewal_date": "2026-07-01", "contract_value": 1},
        ],
    )
    signals = detect_crm_signals(conn, allowed_accounts={"Acme"})
    accounts = [s["account"] for s in signals]
    assert accounts == ["Acme"]


# ── Trace-log assertions ─────────────────────────────────────────────────────

def test_log_trace_emits_counts_for_owning_csm(caplog):
    """The function emits a single INFO line carrying the owned-set size and
    the signals_in / signals_out counts, so production digest runs leave a
    grep-able trace of how aggressively the filter dropped data."""
    renewals, health, ctas = _hubspot_inputs()
    conn = _FakeConnector(renewals=renewals, health=health, ctas=ctas)
    with caplog.at_level(logging.INFO, logger="cs-agent.detector"):
        detect_crm_signals(conn, allowed_accounts={"Acme"})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "owned=1" in text
    assert "signals_in=" in text
    assert "signals_out=" in text


def test_log_trace_zero_owner_case(caplog):
    """For the fail-closed zero-owner case, the trace must report
    owned=0 and signals_out=0 so this branch is unmistakable in logs."""
    renewals, health, ctas = _hubspot_inputs()
    conn = _FakeConnector(renewals=renewals, health=health, ctas=ctas)
    with caplog.at_level(logging.INFO, logger="cs-agent.detector"):
        signals = detect_crm_signals(conn, allowed_accounts=set())
    assert signals == []
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "owned=0" in text
    assert "signals_out=0" in text
