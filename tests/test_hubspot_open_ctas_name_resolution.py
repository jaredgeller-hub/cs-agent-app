"""
tests/test_hubspot_open_ctas_name_resolution.py

HubSpot open CTAs (tickets) must carry the company *name* as account_name
so they join to accounts by name. A CTA whose company ID cannot be
resolved is dropped, and only IDs (never ticket subjects or other record
content) are logged. Uses the same resolution helper as get_renewals.

Standalone-runnable:
    python3 tests/test_hubspot_open_ctas_name_resolution.py
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


def _connector(tickets: list[dict]) -> HubSpotCRMConnector:
    conn = HubSpotCRMConnector("user_test")
    conn._mock = False
    conn._access_token = "fake"
    conn._tool_names = {"search_crm_objects": ""}
    conn._list_tools = AsyncMock(return_value=conn._tool_names)
    conn._mcp_call_tool = AsyncMock(return_value=([_text_block(tickets)], SourceStatus.OK))
    conn._accounts_cache = [
        {"id": "111", "hubspot_id": 111, "name": "Meridian Health", "tier": "standard", "owner": ""},
    ]
    conn._accounts_status = SourceStatus.OK
    return conn


def _ticket(ticket_id: str, company_id: str | None, subject: str) -> dict:
    due = (date.today() - timedelta(days=2)).isoformat()
    item = {
        "id": ticket_id,
        "properties": {
            "subject": subject,
            "hs_ticket_priority_date": due,
            "hs_ticket_priority": "HIGH",
            "hubspot_owner_id": "42",
        },
    }
    if company_id is not None:
        item["associations"] = {"companies": {"results": [{"id": company_id}]}}
    return item


def test_cta_company_id_resolves_to_company_name():
    conn = _connector([_ticket("7001", "111", "Send QBR deck")])
    ctas = conn.get_open_ctas()
    assert len(ctas) == 1
    c = ctas[0]
    assert c["account_name"] == "Meridian Health"
    assert c["title"] == "Send QBR deck"
    assert c["status"] == "overdue"
    assert c["priority"] == "high"
    # Internal join keys do not leak into the public shape.
    assert set(c) == {"account_name", "title", "due_date", "priority", "status", "owner"}


def test_cta_unresolvable_company_id_is_skipped_and_logs_ids_only(caplog):
    conn = _connector([
        _ticket("7002", "999", "Confidential escalation for Secret Co"),
        _ticket("7001", "111", "Send QBR deck"),
    ])
    with caplog.at_level(logging.DEBUG, logger="cs-agent.hubspot_crm"):
        ctas = conn.get_open_ctas()
    assert [c["account_name"] for c in ctas] == ["Meridian Health"]
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "7002" in log_text and "999" in log_text
    assert "Confidential" not in log_text
    assert "Secret Co" not in log_text


def test_cta_without_company_association_is_skipped(caplog):
    conn = _connector([_ticket("7003", None, "Orphan ticket subject")])
    with caplog.at_level(logging.DEBUG, logger="cs-agent.hubspot_crm"):
        ctas = conn.get_open_ctas()
    assert ctas == []
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "7003" in log_text
    assert "Orphan ticket subject" not in log_text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
