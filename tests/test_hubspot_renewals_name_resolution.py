"""
tests/test_hubspot_renewals_name_resolution.py

HubSpot renewals must carry the company *name* as account_name so they
join to accounts by name. A renewal whose company ID cannot be resolved
is dropped, and only IDs (never deal names or other record content) are
logged.

Standalone-runnable:
    python3 tests/test_hubspot_renewals_name_resolution.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.connectors.hubspot_crm import HubSpotCRMConnector
from product.connectors.source_status import SourceStatus


@pytest.fixture(autouse=True)
def _skip_token_load(monkeypatch):
    monkeypatch.delenv("HUBSPOT_USE_MOCK", raising=False)
    with patch.object(HubSpotCRMConnector, "_load_and_refresh_token", return_value=None):
        yield


def _text_block(payload) -> MagicMock:
    block = MagicMock()
    block.text = json.dumps(payload)
    return block


def _connector(deals: list[dict]) -> HubSpotCRMConnector:
    conn = HubSpotCRMConnector("user_test")
    conn._mock = False
    conn._access_token = "fake"
    conn._tool_names = {"search_crm_objects": ""}
    conn._list_tools = AsyncMock(return_value=conn._tool_names)
    conn._mcp_call_tool = AsyncMock(return_value=([_text_block(deals)], SourceStatus.OK))
    conn._accounts_cache = [
        {"id": "111", "hubspot_id": 111, "name": "Meridian Health", "tier": "standard", "owner": ""},
    ]
    conn._accounts_status = SourceStatus.OK
    return conn


def _deal(deal_id: str, company_id: str | None, dealname: str) -> dict:
    close = (date.today() + timedelta(days=30)).isoformat()
    item = {
        "id": deal_id,
        "properties": {"dealname": dealname, "closedate": close, "amount": "50000"},
    }
    if company_id is not None:
        item["associations"] = {"companies": {"results": [{"id": company_id}]}}
    return item


def test_company_id_resolves_to_company_name():
    conn = _connector([_deal("9001", "111", "Meridian renewal FY27")])
    renewals = conn.get_renewals()
    assert len(renewals) == 1
    r = renewals[0]
    assert r["account_name"] == "Meridian Health"
    assert r["contract_value"] == "50000"
    assert r["days_until_renewal"] == 30
    # Internal join keys do not leak into the public shape.
    assert set(r) == {"account_name", "renewal_date", "contract_value", "days_until_renewal"}


def test_unresolvable_company_id_is_skipped_and_logs_ids_only(caplog):
    conn = _connector([
        _deal("9002", "999", "Secret Co confidential expansion"),
        _deal("9001", "111", "Meridian renewal FY27"),
    ])
    with caplog.at_level(logging.DEBUG, logger="cs-agent.hubspot_crm"):
        renewals = conn.get_renewals()
    assert [r["account_name"] for r in renewals] == ["Meridian Health"]
    assert all(r["account_name"] != "999" for r in renewals)
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "999" in log_text and "9002" in log_text
    assert "Secret Co" not in log_text
    assert "confidential" not in log_text
    assert "50000" not in log_text


def test_deal_without_company_association_is_skipped_not_named_by_dealname(caplog):
    conn = _connector([_deal("9003", None, "Orphan deal name")])
    with caplog.at_level(logging.DEBUG, logger="cs-agent.hubspot_crm"):
        renewals = conn.get_renewals()
    assert renewals == []
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "9003" in log_text
    assert "Orphan deal name" not in log_text


def test_lookup_company_name_returns_none_on_miss():
    conn = _connector([])
    assert conn._lookup_company_name("111") == "Meridian Health"
    assert conn._lookup_company_name("999") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
