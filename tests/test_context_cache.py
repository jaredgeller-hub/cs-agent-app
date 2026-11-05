"""tests/test_context_cache.py

Track C from Sprint 5 Week 1: cap behavior at scale for product/context_cache.

Verifies that the per-account, snapshot-level, and summary-text caps in
product.context_cache hold under inputs that exceed the cap, not just at
realistic-today portfolio size. Caps exist to defend against growth, so
fixtures are sized at multiples of the cap (100 elements for per-account
caps, 500 elements for snapshot-level caps).

Coverage matrix:
  Per-account caps in the cache write loop
    - acct_contacts[:3]  -> snapshot per-account contacts capped at 3
    - acct_deals[:5]     -> snapshot per-account deals capped at 5
    - acct_tickets[:5]   -> snapshot per-account tickets capped at 5

  Snapshot-level caps at snapshot assembly
    - deals[:20], tickets[:20], contacts[:60] (HubSpot path)
    - health_scores[:20], success_plans[:20] (Gainsight path)

  Summary text builder caps in _build_summary_text
    - deal_strs[:5], all_accounts[:15], acct_summaries[:10],
      acct_contacts_for[:2], high_tickets[:5], at_risk[:5]

  SourceStatus minimal interaction
    - source_status.by_account field present after a successful refresh
    - _build_summary_text legacy path (no account_status) byte-identical
      to account_status={} at scale

Standalone-runnable:
    python3 tests/test_context_cache.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.connectors.source_status import SourceStatus
from product.context_cache import _build_summary_text


# ── Fixture builders ────────────────────────────────────────────────────────


def _build_contact_payload(n: int, account_label: str = "Source") -> list[dict]:
    """Build n connector-shaped contact dicts. Names look like real HubSpot data."""
    first_names = ["Marcus", "Priya", "Dana", "Alex", "Jordan", "Riley", "Sam", "Taylor"]
    last_names = ["Reyes", "Sharma", "Mills", "Watson", "Chen", "Park", "Nguyen", "Patel"]
    return [
        {
            "name": f"{first_names[i % len(first_names)]} {last_names[(i // 8) % len(last_names)]} {i:03d}",
            "email": f"contact{i:03d}@{account_label.lower().replace(' ', '')}.com",
            "role": "VP Operations" if i % 3 == 0 else "Director",
        }
        for i in range(n)
    ]


def _build_deal_payload(n: int) -> list[dict]:
    """Build n connector-shaped deal dicts, all closing within 180 days of 2026-05-08."""
    return [
        {
            "name": f"Renewal {i:03d}",
            "stage": "open" if i % 2 == 0 else "closed_won",
            "amount": 50000 + (i * 1000),
            "close_date": "2026-08-01",
        }
        for i in range(n)
    ]


def _build_ticket_payload(n: int) -> list[dict]:
    """Build n connector-shaped ticket dicts. Half marked HIGH priority."""
    return [
        {
            "subject": f"Integration issue {i:03d}",
            "status": "OPEN" if i % 2 == 0 else "WAITING",
            "priority": "HIGH" if i % 2 == 0 else "MEDIUM",
        }
        for i in range(n)
    ]


def _build_health_score_payload(n: int) -> list[dict]:
    """Build n Gainsight-shaped health score dicts, all 'declining' so they hit at_risk."""
    return [
        {
            "account_name": f"Health Account {i:03d}",
            "trend": "declining",
            "color_code": "red" if i % 2 == 0 else "yellow",
            "score": 40 + (i % 30),
        }
        for i in range(n)
    ]


def _build_success_plan_payload(n: int) -> list[dict]:
    return [
        {
            "title": f"Onboarding Plan {i:03d}",
            "status": "in_progress" if i % 2 == 0 else "complete",
            "completion": (i * 7) % 100,
        }
        for i in range(n)
    ]


def _build_account_payload(n: int, with_hubspot_id: bool = True) -> list[dict]:
    """Build n connector-shaped account dicts."""
    industries = ["Healthcare", "Finance", "Retail", "Logistics", "Technology"]
    if with_hubspot_id:
        return [
            {
                "id": f"acct-{i:04d}",
                "hubspot_id": 100000 + i,
                "name": f"{industries[i % len(industries)]} Co {i:03d}",
                "owner_id": "owner-001",
            }
            for i in range(n)
        ]
    return [
        {
            "id": f"acct-{i:04d}",
            "name": f"{industries[i % len(industries)]} Co {i:03d}",
            "owner_id": "owner-001",
            "status": "active",
        }
        for i in range(n)
    ]


# ── Module-scoped fixtures (built once, reused across tests) ────────────────


@pytest.fixture(scope="module")
def hundred_contacts() -> list[dict]:
    return _build_contact_payload(100)


@pytest.fixture(scope="module")
def hundred_deals() -> list[dict]:
    return _build_deal_payload(100)


@pytest.fixture(scope="module")
def hundred_tickets() -> list[dict]:
    return _build_ticket_payload(100)


@pytest.fixture(scope="module")
def five_hundred_health_scores() -> list[dict]:
    return _build_health_score_payload(500)


@pytest.fixture(scope="module")
def five_hundred_success_plans() -> list[dict]:
    return _build_success_plan_payload(500)


# ── HubSpot connector mock harness ──────────────────────────────────────────


def _make_hubspot_connector(
    accounts: list[dict],
    contacts_by_acct: dict[int, list[dict]],
    deals_by_acct: dict[int, list[dict]],
    tickets_by_acct: dict[int, list[dict]],
) -> MagicMock:
    """Build a HubSpot-shaped MagicMock connector with canned per-account returns.

    Status is OK for non-empty payloads, EMPTY otherwise.
    """
    connector = MagicMock(name="HubSpotCRMConnector")
    connector._get_accounts_async = AsyncMock(return_value=accounts)

    async def _contacts(hid: int) -> tuple[list[dict], SourceStatus]:
        items = contacts_by_acct.get(int(hid), [])
        return items, SourceStatus.OK if items else SourceStatus.EMPTY

    async def _deals(hid: int) -> tuple[list[dict], SourceStatus]:
        items = deals_by_acct.get(int(hid), [])
        return items, SourceStatus.OK if items else SourceStatus.EMPTY

    async def _tickets(hid: int) -> tuple[list[dict], SourceStatus]:
        items = tickets_by_acct.get(int(hid), [])
        return items, SourceStatus.OK if items else SourceStatus.EMPTY

    connector._get_contacts_by_id_async = AsyncMock(side_effect=_contacts)
    connector._get_deals_for_company_async = AsyncMock(side_effect=_deals)
    connector._get_tickets_for_company_async = AsyncMock(side_effect=_tickets)

    @asynccontextmanager
    async def _batch():
        yield connector

    connector.batch = _batch
    return connector


def _make_gainsight_connector(
    accounts: list[dict],
    contacts_by_acct: dict[str, list[dict]] | None = None,
    success_plans_by_acct: dict[str, list[dict]] | None = None,
    renewals: list[dict] | None = None,
    open_ctas: list[dict] | None = None,
    health_scores: list[dict] | None = None,
) -> MagicMock:
    """Build a Gainsight-shaped MagicMock connector with canned returns."""
    connector = MagicMock(name="GainsightCRMConnector")
    connector._get_accounts_async = AsyncMock(return_value=accounts)

    contacts_by_acct = contacts_by_acct or {}
    success_plans_by_acct = success_plans_by_acct or {}

    async def _contacts(name: str) -> list[dict]:
        return contacts_by_acct.get(name, [])

    async def _success_plans(name: str) -> list[dict]:
        return success_plans_by_acct.get(name, [])

    connector._get_contacts_async = AsyncMock(side_effect=_contacts)
    connector._get_success_plans_async = AsyncMock(side_effect=_success_plans)
    connector._get_renewals_async = AsyncMock(return_value=renewals or [])
    connector._get_open_ctas_async = AsyncMock(return_value=open_ctas or [])
    connector._get_health_scores_async = AsyncMock(return_value=health_scores or [])

    @asynccontextmanager
    async def _batch():
        yield connector

    connector.batch = _batch
    return connector


def _run_refresh_capture_snapshot(
    *,
    csm_id: str,
    connector: MagicMock,
    crm: str = "hubspot",
) -> dict:
    """Run refresh_user_context against a mocked connector and return the snapshot dict."""
    fake_session = MagicMock()
    fake_session.execute = MagicMock()
    fake_session.commit = MagicMock()
    fake_session.rollback = MagicMock()
    fake_session.close = MagicMock()
    fake_session.query.return_value.filter_by.return_value.first.return_value = None
    fake_session_factory = MagicMock(return_value=fake_session)

    real_dumps = json.dumps
    captured: dict = {}

    def _capturing_dumps(obj, *args, **kwargs):
        if isinstance(obj, dict) and "source_status" in obj:
            captured["snapshot"] = obj
        return real_dumps(obj, *args, **kwargs)

    if crm == "hubspot":
        token_patches = [
            patch("product.connectors.gainsight_mcp.load_gainsight_token", return_value=None),
            patch(
                "product.connectors.hubspot_mcp.load_hubspot_token",
                return_value={"access_token": "fake", "expires_at": 9999999999},
            ),
            patch("product.connectors.hubspot_crm.HubSpotCRMConnector", return_value=connector),
        ]
    else:  # gainsight
        token_patches = [
            patch(
                "product.connectors.gainsight_mcp.load_gainsight_token",
                return_value={"access_token": "fake", "expires_at": 9999999999},
            ),
            patch("product.connectors.gainsight.GainsightCRMConnector", return_value=connector),
        ]

    with patch("product.database.SessionLocal", fake_session_factory), \
         patch("product.context_cache.json.dumps", side_effect=_capturing_dumps), \
         patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        for p in token_patches:
            p.start()
        try:
            from product import context_cache as cc
            asyncio.run(cc.refresh_user_context(csm_id))
        finally:
            for p in token_patches:
                p.stop()

    assert "snapshot" in captured, "snapshot was never persisted"
    return captured["snapshot"]


# ── Per-account caps ────────────────────────────────────────────────────────


def test_per_account_contacts_capped_at_three(hundred_contacts: list[dict]) -> None:
    """100 contacts returned for one account -> snapshot has 3 contacts for that account."""
    accounts = _build_account_payload(1)
    hid = accounts[0]["hubspot_id"]
    connector = _make_hubspot_connector(
        accounts=accounts,
        contacts_by_acct={hid: hundred_contacts},
        deals_by_acct={},
        tickets_by_acct={},
    )

    snapshot = _run_refresh_capture_snapshot(csm_id="cs_track_c_contacts_per_acct", connector=connector)

    assert len(snapshot["contacts"]) == 3
    assert all(c["account_name"] == accounts[0]["name"] for c in snapshot["contacts"])
    assert snapshot["contacts"][0]["email"] == hundred_contacts[0]["email"]


def test_per_account_deals_capped_at_five(hundred_deals: list[dict]) -> None:
    """100 deals returned for one account -> snapshot has 5 deals (after [:5] cap)."""
    accounts = _build_account_payload(1)
    hid = accounts[0]["hubspot_id"]
    connector = _make_hubspot_connector(
        accounts=accounts,
        contacts_by_acct={},
        deals_by_acct={hid: hundred_deals},
        tickets_by_acct={},
    )

    snapshot = _run_refresh_capture_snapshot(csm_id="cs_track_c_deals_per_acct", connector=connector)

    assert len(snapshot["deals"]) == 5
    assert snapshot["deals"][0]["name"] == "Renewal 000"


def test_per_account_tickets_capped_at_five(hundred_tickets: list[dict]) -> None:
    """100 tickets returned for one account -> snapshot has 5 tickets."""
    accounts = _build_account_payload(1)
    hid = accounts[0]["hubspot_id"]
    connector = _make_hubspot_connector(
        accounts=accounts,
        contacts_by_acct={},
        deals_by_acct={},
        tickets_by_acct={hid: hundred_tickets},
    )

    snapshot = _run_refresh_capture_snapshot(csm_id="cs_track_c_tickets_per_acct", connector=connector)

    assert len(snapshot["tickets"]) == 5
    assert snapshot["tickets"][0]["subject"] == "Integration issue 000"


# ── Snapshot-level caps (HubSpot path) ──────────────────────────────────────


def test_snapshot_deals_capped_at_twenty(hundred_deals: list[dict]) -> None:
    """20 accounts * 5 deals each = 100 accumulated deals -> snapshot caps at 20."""
    accounts = _build_account_payload(20)
    deals_by_acct = {a["hubspot_id"]: hundred_deals for a in accounts}
    connector = _make_hubspot_connector(
        accounts=accounts,
        contacts_by_acct={},
        deals_by_acct=deals_by_acct,
        tickets_by_acct={},
    )

    snapshot = _run_refresh_capture_snapshot(csm_id="cs_track_c_snapshot_deals", connector=connector)

    assert len(snapshot["deals"]) == 20


def test_snapshot_tickets_capped_at_twenty(hundred_tickets: list[dict]) -> None:
    """20 accounts * 5 tickets each = 100 accumulated tickets -> snapshot caps at 20."""
    accounts = _build_account_payload(20)
    tickets_by_acct = {a["hubspot_id"]: hundred_tickets for a in accounts}
    connector = _make_hubspot_connector(
        accounts=accounts,
        contacts_by_acct={},
        deals_by_acct={},
        tickets_by_acct=tickets_by_acct,
    )

    snapshot = _run_refresh_capture_snapshot(csm_id="cs_track_c_snapshot_tickets", connector=connector)

    assert len(snapshot["tickets"]) == 20


def test_snapshot_contacts_within_sixty(hundred_contacts: list[dict]) -> None:
    """20 accounts * 3 contacts/account = 60. Snapshot guard caps at 60.

    The [:60] cap at snapshot assembly is currently redundant because the
    per-account [:3] cap times the [:20] account-detail cap yields exactly
    60. This test pins the contract: snapshot.contacts is never longer
    than 60, regardless of how the upstream slices interact in the future.
    """
    accounts = _build_account_payload(20)
    contacts_by_acct = {a["hubspot_id"]: hundred_contacts for a in accounts}
    connector = _make_hubspot_connector(
        accounts=accounts,
        contacts_by_acct=contacts_by_acct,
        deals_by_acct={},
        tickets_by_acct={},
    )

    snapshot = _run_refresh_capture_snapshot(csm_id="cs_track_c_snapshot_contacts", connector=connector)

    assert len(snapshot["contacts"]) <= 60
    assert len(snapshot["contacts"]) == 60


# ── Snapshot-level caps (Gainsight path: health_scores, success_plans) ──────


def test_snapshot_gainsight_health_and_success_plans_capped_at_twenty(
    five_hundred_health_scores: list[dict],
    five_hundred_success_plans: list[dict],
) -> None:
    """500 health scores (global) and 500 success plans (per-account) each cap at 20.

    These caps are the explicit snapshot-level guards at context_cache.py
    lines 322-323 and only get exercised on the Gainsight branch.
    """
    accounts = _build_account_payload(1, with_hubspot_id=False)
    acct_name = accounts[0]["name"]
    scoped_health = [
        {**h, "account_name": acct_name} for h in five_hundred_health_scores
    ]
    connector = _make_gainsight_connector(
        accounts=accounts,
        success_plans_by_acct={acct_name: five_hundred_success_plans},
        health_scores=scoped_health,
    )

    snapshot = _run_refresh_capture_snapshot(
        csm_id="cs_track_c_snapshot_gs_caps",
        connector=connector,
        crm="gainsight",
    )

    assert len(snapshot["health_scores"]) == 20
    assert len(snapshot["success_plans"]) == 20


# ── Summary text builder caps (pure function) ───────────────────────────────


def _summary_inputs_at_scale(
    *,
    accounts: int = 0,
    deals: int = 0,
    contacts_for_first_account: int = 0,
    tickets: int = 0,
    health_scores: int = 0,
) -> dict:
    """Compose _build_summary_text kwargs sized to exercise specific caps."""
    accts = _build_account_payload(accounts) if accounts else []
    first_name = accts[0]["name"] if accts else "Meridian Health"

    deal_list = [
        {
            "account_name": first_name,
            "name": f"Renewal {i:03d}",
            "stage": "open",
            "amount": 50000 + i,
            "close_date": "2026-08-01",
            "days_until_close": (i % 180),
        }
        for i in range(deals)
    ]

    contact_list = [
        {"account_name": first_name, "name": f"Contact {i:03d}", "role": "VP"}
        for i in range(contacts_for_first_account)
    ]

    ticket_list = [
        {
            "account_name": first_name,
            "subject": f"Issue {i:03d}",
            "status": "OPEN",
            "priority": "high",
        }
        for i in range(tickets)
    ]

    health_list = [
        {
            "account_name": f"Health Acct {i:03d}",
            "trend": "declining",
            "color_code": "red",
        }
        for i in range(health_scores)
    ]

    return dict(
        all_accounts=accts,
        deals=deal_list,
        contacts=contact_list,
        tickets=ticket_list,
        health_scores=health_list,
    )


def test_summary_deal_strs_capped_at_five() -> None:
    """100 deals in input -> summary lists exactly 5 deal strings, header reports total."""
    inputs = _summary_inputs_at_scale(accounts=1, deals=100)

    text = _build_summary_text(account_status=None, **inputs)

    assert "Key deals (100 total): " in text
    deals_section = text.split("Key deals (100 total): ")[1].split(". ")[0]
    assert deals_section.count(";") == 4


def test_summary_all_accounts_and_summaries_capped() -> None:
    """100 accounts in input -> only the first 10 appear in the joined summary line.

    Covers both all_accounts[:15] (iteration cap) and acct_summaries[:10]
    (join cap). The visible behavior is that account index 9 appears and
    account index 10 does not, since the join slices [:10].
    """
    inputs = _summary_inputs_at_scale(accounts=100)

    text = _build_summary_text(account_status=None, **inputs)

    accts = inputs["all_accounts"]
    assert accts[0]["name"] in text
    assert accts[9]["name"] in text
    assert accts[10]["name"] not in text
    assert accts[15]["name"] not in text


def test_summary_acct_contacts_for_capped_at_two() -> None:
    """100 contacts for one account -> summary shows at most 2 contact names for it."""
    inputs = _summary_inputs_at_scale(accounts=1, contacts_for_first_account=100)

    text = _build_summary_text(account_status=None, **inputs)

    contacts = inputs["contacts"]
    assert contacts[0]["name"] in text
    assert contacts[1]["name"] in text
    assert contacts[2]["name"] not in text
    assert contacts[50]["name"] not in text


def test_summary_high_tickets_capped_at_five() -> None:
    """100 high-priority tickets -> summary shows exactly 5 in the high-priority line."""
    inputs = _summary_inputs_at_scale(accounts=1, tickets=100)

    text = _build_summary_text(account_status=None, **inputs)

    assert "High-priority tickets:" in text
    ticket_line = text.split("High-priority tickets: ")[1].split(". ")[0]
    assert ticket_line.count(";") == 4


def test_summary_at_risk_health_capped_at_five() -> None:
    """100 declining health scores -> summary shows total count and lists 5 names."""
    inputs = _summary_inputs_at_scale(accounts=1, health_scores=100)

    text = _build_summary_text(account_status=None, **inputs)

    assert "Health at risk (100):" in text
    health_line = text.split("Health at risk (100): ")[1]
    assert health_line.count(";") == 4


# ── SourceStatus minimal interaction ────────────────────────────────────────


def test_snapshot_includes_source_status_field(hundred_contacts: list[dict]) -> None:
    """A successful refresh produces a snapshot with source_status.by_account shaped as a dict."""
    accounts = _build_account_payload(2)
    hid_a, hid_b = accounts[0]["hubspot_id"], accounts[1]["hubspot_id"]
    connector = _make_hubspot_connector(
        accounts=accounts,
        contacts_by_acct={hid_a: hundred_contacts},
        deals_by_acct={},
        tickets_by_acct={hid_b: _build_ticket_payload(5)},
    )

    snapshot = _run_refresh_capture_snapshot(csm_id="cs_track_c_status_field", connector=connector)

    assert "source_status" in snapshot
    assert isinstance(snapshot["source_status"]["by_account"], dict)
    assert str(hid_a) in snapshot["source_status"]["by_account"]
    assert str(hid_b) in snapshot["source_status"]["by_account"]


def test_summary_text_legacy_path_byte_identical_at_scale() -> None:
    """At scale (100 accounts, 100 deals), account_status=None == account_status={}.

    Defensive default for legacy snapshots that lack source_status. Verifies
    the no-status path is byte-identical to the explicit empty-status path
    even when caps are heavily exercised.
    """
    inputs = _summary_inputs_at_scale(
        accounts=100,
        deals=100,
        contacts_for_first_account=100,
        tickets=100,
        health_scores=100,
    )

    text_legacy = _build_summary_text(account_status=None, **inputs)
    text_empty = _build_summary_text(account_status={}, **inputs)

    assert text_legacy == text_empty
    assert "Data fetch issues" not in text_legacy


def main_runner() -> None:
    """Standalone runner."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
