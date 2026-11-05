"""
tests/test_absence_detection_renewal_plumbing.py

Regression guards for Track D[F7] (Sprint 5 Week 1):

The 14-day zero-alerts state was caused by account_profiles[acct]["renewal_days"]
being hardcoded None in main._run_absence_detection. Achievable max urgency was
0.50 against a 0.65 alert gate, so the absence cron ran correctly but
mathematically could not fire alerts.

After F7, _run_absence_detection fetches connector.get_renewals() once per CSM,
builds {account_name: days_until_renewal}, and populates account_profiles from
the lookup. On fetch failure it logs a WARNING and falls through to None for
all accounts (prior degraded behavior, now observable).

Tests in this file:
  1. get_renewals returns data → renewal_days populated per account.
  2. get_renewals raises → falls back to None for all accounts; WARNING fires.
  3. Account in account_contacts but missing from get_renewals response →
     missing one gets None, present one gets the correct value.
  4. Empty get_renewals response ([]) → all accounts get None, no crash.
  5. Math verification: with renewal_days=25 plumbed, score_urgency yields a
     value >= 0.65 given realistic silence input. This is the regression test
     that proves F7 actually unblocks alerts.

Standalone-runnable:
    python3 tests/test_absence_detection_renewal_plumbing.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import Base, Connection, Customer
import product.connectors.registry as _registry
import product.org_config as _org_config
import product.signals.detector as _detector
import product.signals.gmail as _gmail
from tests._utils import seed_csm_with_gmail_scope


# ── In-memory SQLite for the Phase 2 scope gate ──────────────────────────────
# _run_absence_detection now calls get_connection_state at loop entry.
# Without a seeded Connection row, the gate skips the CSM and the test
# never reaches the renewal-plumbing code under test. The stub_pipeline
# fixture below seeds GMAIL_READONLY so the gate passes.

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
        s.query(Connection).delete()
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


# ── Fake connector and capture helpers ───────────────────────────────────────

class _FakeConnector:
    """Minimal CRMConnector-shaped fake. Only the methods touched by
    _run_absence_detection are implemented."""

    def __init__(
        self,
        accounts: list[dict],
        contacts_by_account: dict[str, list[dict]],
        renewals: list[dict] | None = None,
        renewals_raises: BaseException | None = None,
    ) -> None:
        self._accounts = accounts
        self._contacts_by_account = contacts_by_account
        self._renewals = renewals or []
        self._renewals_raises = renewals_raises
        self.get_renewals_calls = 0

    def get_accounts(self) -> list[dict]:
        return self._accounts

    def get_contacts(self, account_name: str) -> list[dict]:
        return self._contacts_by_account.get(account_name, [])

    def get_renewals(self) -> list[dict]:
        self.get_renewals_calls += 1
        if self._renewals_raises is not None:
            raise self._renewals_raises
        return self._renewals


class _Capture:
    """Captures the third argument (account_profiles) passed to
    detect_absence_signals so the test can assert on its contents."""

    def __init__(self) -> None:
        self.account_profiles: dict | None = None
        self.calls = 0

    def __call__(self, gmail_signals, event_history, account_profiles):
        self.calls += 1
        self.account_profiles = account_profiles
        return []


# ── Fixture: stub the modules _run_absence_detection imports ─────────────────

@pytest.fixture
def stub_pipeline(monkeypatch, tmp_path):
    """Wires fake org_config, fake gmail extraction, and a Capture for
    detect_absence_signals. Returns a configurator that the caller uses to
    install a connector and run.

    Uses tmp_path as cwd so any shadow-mode alert disk writes land outside
    the repo.
    """
    monkeypatch.chdir(tmp_path)

    csm_id = "test_csm"
    accounts_cfg = ["Northgate", "Acme"]

    # Phase 2 scope gate now lives at the top of the loop; seed a
    # Connection row with gmail.readonly so the gate passes and these
    # downstream renewal-plumbing assertions still get reached.
    s = _TestSession()
    try:
        seed_csm_with_gmail_scope(s, csm_id)
    finally:
        s.close()

    monkeypatch.setattr(_org_config, "list_active_csms", lambda: [csm_id])
    monkeypatch.setattr(
        _org_config,
        "get_csm_config",
        lambda _id: {"name": "Test CSM", "email": "t@x.example", "accounts": accounts_cfg},
    )
    monkeypatch.setattr(_org_config, "get_crm_config", lambda: {"crm_type": "hubspot"})
    monkeypatch.setattr(_org_config, "get_pilot_mode", lambda: "shadow")

    monkeypatch.setattr(_gmail, "extract_gmail_signals", lambda _csm, _ac: [])

    capture = _Capture()
    monkeypatch.setattr(_detector, "detect_absence_signals", capture)

    def _configure(connector: _FakeConnector) -> _Capture:
        monkeypatch.setattr(_registry, "get_connector", lambda *_a, **_k: connector)
        return capture

    return _configure


def _run() -> None:
    """Import-and-run helper. Imports _run_absence_detection lazily so the
    monkeypatches above are in place before main resolves the name."""
    from main import _run_absence_detection
    _run_absence_detection()


def _make_connector(renewals=None, renewals_raises=None) -> _FakeConnector:
    return _FakeConnector(
        accounts=[{"name": "Northgate"}, {"name": "Acme"}],
        contacts_by_account={
            "Northgate": [{"email": "dana@northgate.example"}],
            "Acme":      [{"email": "ana@acme.example"}],
        },
        renewals=renewals,
        renewals_raises=renewals_raises,
    )


# ── 1. Success: renewal_days populated from get_renewals ─────────────────────

def test_get_renewals_populates_renewal_days(stub_pipeline):
    capture = stub_pipeline(_make_connector(renewals=[
        {"account_name": "Northgate", "days_until_renewal": 25},
        {"account_name": "Acme",      "days_until_renewal": 90},
    ]))

    _run()

    assert capture.calls == 1
    profiles = capture.account_profiles
    assert profiles is not None
    assert profiles["Northgate"]["renewal_days"] == 25
    assert profiles["Acme"]["renewal_days"] == 90


# ── 2. Failure: get_renewals raises → None for all + WARNING log ─────────────

def test_get_renewals_raises_falls_back_to_none_and_warns(stub_pipeline, caplog):
    capture = stub_pipeline(_make_connector(renewals_raises=RuntimeError("mcp boom")))

    with caplog.at_level(logging.WARNING):
        _run()

    profiles = capture.account_profiles
    assert profiles is not None
    assert profiles["Northgate"]["renewal_days"] is None
    assert profiles["Acme"]["renewal_days"] is None

    matching = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING and "get_renewals failed" in rec.getMessage()
    ]
    assert matching, "expected a WARNING log on get_renewals failure"


# ── 3. Partial response: missing account → None, present → value ─────────────

def test_get_renewals_partial_response(stub_pipeline):
    capture = stub_pipeline(_make_connector(renewals=[
        {"account_name": "Northgate", "days_until_renewal": 25},
    ]))

    _run()

    profiles = capture.account_profiles
    assert profiles is not None
    assert profiles["Northgate"]["renewal_days"] == 25
    assert profiles["Acme"]["renewal_days"] is None


# ── 4. Empty get_renewals response ───────────────────────────────────────────

def test_get_renewals_empty_response(stub_pipeline):
    capture = stub_pipeline(_make_connector(renewals=[]))

    _run()

    profiles = capture.account_profiles
    assert profiles is not None
    assert profiles["Northgate"]["renewal_days"] is None
    assert profiles["Acme"]["renewal_days"] is None


# ── 5. Math: renewal_days plumbed produces urgency >= 0.65 ───────────────────

def test_score_urgency_crosses_alert_gate_with_renewal_plumbed():
    """With renewal_days=25 and silence ratio at the 3x norm threshold, the
    weighted urgency must clear the 0.65 alert gate. Pre-F7, renewal_days=None
    capped this same input at 0.50, so no alert could fire."""
    from product.signals.detector import score_urgency

    signal = {"signal_type": "absence.relationship_silence"}

    # Realistic input: renewal in 25 days, contact silent ~9 days against a
    # 3-day norm (silence ratio 3.0 → silence_score 1.0). signal_count=2 models
    # the realistic pairing produced by detect_absence_signals when a single
    # silent contact crosses both the relationship_silence and action_silence
    # detectors (same email.absence input, two emitted signals).
    account_context = {
        "renewal_days":              25,
        "days_silent":               9,
        "contact_avg_response_days": 3.0,
        "signal_count":              2,
        "cta_overdue":               False,
    }

    urgency = score_urgency(signal, account_context)
    assert urgency >= 0.65, f"expected urgency >= 0.65 with F7 plumbed, got {urgency}"

    # And the regression directly: with renewal_days=None on the same input,
    # urgency must be below the gate. This is exactly the pre-F7 behavior.
    account_context_pre_f7 = dict(account_context, renewal_days=None)
    urgency_pre_f7 = score_urgency(signal, account_context_pre_f7)
    assert urgency_pre_f7 < 0.65, (
        f"sanity: with renewal_days=None, urgency must stay below the gate; "
        f"got {urgency_pre_f7}"
    )


# ── Standalone runnable ──────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
