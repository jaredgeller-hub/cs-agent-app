"""
tests/test_hubspot_recent_activity_notes.py

Regression guards for F-2: notes logged via HubSpotWriter.log_note must be
surfaced by get_account_dossier's recent_activity.

The bug: _get_recent_activity_async queried objectType "engagements" with a
name-string `associations` filter (violating the numeric-ID association rule,
CLAUDE.md #9) and read hs_body_preview. A note created by log_note is a
`notes` object associated note_to_company, with its text in hs_note_body — so
it was never returned.

The fix: query objectType "notes" with the associatedWith + objectIdValues
numeric filter (same pattern as contacts/deals/tickets) and read hs_note_body.

These tests exercise the query shape + parsing directly (the dossier unit
test in test_chat_tool_dossier.py mocks this method, so it cannot catch a
query-shape regression).

Standalone-runnable:
    python3 tests/test_hubspot_recent_activity_notes.py
"""

import asyncio
import json
import sys
import types
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.connectors.hubspot_crm import HubSpotCRMConnector
from product.connectors.source_status import SourceStatus


def _content(payload) -> list:
    """Wrap a JSON-serializable payload as the MCP content-block list shape
    (_text_from_content reads `.text` off each block)."""
    return [types.SimpleNamespace(text=json.dumps(payload))]


def _make_connector() -> HubSpotCRMConnector:
    """A connector built in mock env (skips token load), then flipped to
    non-mock so the real MCP code path runs against patched helpers."""
    import os
    os.environ["HUBSPOT_USE_MOCK"] = "true"
    try:
        c = HubSpotCRMConnector("user_x")
    finally:
        os.environ.pop("HUBSPOT_USE_MOCK", None)
    c._mock = False
    c._access_token = "fake"
    c._tool_names = {"hubspot-search_crm-objects": "search the CRM"}
    return c


def test_recent_activity_queries_notes_with_numeric_association():
    """The query targets objectType 'notes' via associatedWith + numeric ID."""
    c = _make_connector()
    captured = {}

    async def _fake_list_tools():
        return c._tool_names

    async def _fake_call_tool(tool_name, arguments):
        captured["tool"] = tool_name
        captured["args"] = arguments
        return _content({"results": []}), SourceStatus.OK

    c._list_tools = _fake_list_tools
    c._mcp_call_tool = _fake_call_tool

    asyncio.run(c._get_recent_activity_async(12345678, 14))

    args = captured["args"]
    assert args["objectType"] == "notes", "must query notes, not engagements"
    assoc = args["filterGroups"][0]["associatedWith"][0]
    assert assoc["objectType"] == "companies"
    assert assoc["objectIdValues"] == [12345678], "must filter by numeric company ID"
    assert "associations" not in args, "must not use the legacy name-string filter"
    assert "hs_note_body" in args["properties"], "must request the note body field"
    print("✓ recent_activity queries notes with numeric associatedWith filter")


def test_recent_activity_parses_note_body_and_type():
    """A returned note maps to type 'note' with summary from hs_note_body."""
    c = _make_connector()
    today_iso = date.today().isoformat()

    async def _fake_list_tools():
        return c._tool_names

    async def _fake_call_tool(tool_name, arguments):
        payload = {"results": [{
            "id": "note_1",
            "properties": {
                "hs_note_body": "Call recap: renewal on track, Dana aligned.",
                "hs_timestamp": today_iso,
                "hubspot_owner_id": "99",
            },
        }]}
        return _content(payload), SourceStatus.OK

    c._list_tools = _fake_list_tools
    c._mcp_call_tool = _fake_call_tool

    out = asyncio.run(c._get_recent_activity_async(12345678, 14))

    assert out == [{
        "type": "note",
        "summary": "Call recap: renewal on track, Dana aligned.",
        "date": today_iso,
        "author": "99",
    }]
    print("✓ recent_activity parses hs_note_body into a type=note entry")


def test_recent_activity_returns_empty_on_failed_status():
    """A FAILED MCP status yields [] (no crash, no partial parse)."""
    c = _make_connector()

    async def _fake_list_tools():
        return c._tool_names

    async def _fake_call_tool(tool_name, arguments):
        return [], SourceStatus.FAILED

    c._list_tools = _fake_list_tools
    c._mcp_call_tool = _fake_call_tool

    assert asyncio.run(c._get_recent_activity_async(12345678, 14)) == []
    print("✓ recent_activity returns [] on FAILED status")


def test_sync_wrapper_resolves_name_to_id():
    """get_recent_activity (ABC, name-based) resolves the name to a numeric
    ID before delegating to the async fetch."""
    c = _make_connector()
    c._accounts_cache = [
        {"id": "1", "hubspot_id": 555, "name": "Acme", "tier": "standard", "owner": ""},
    ]
    c._get_recent_activity_async = AsyncMock(return_value=[
        {"type": "note", "summary": "x", "date": "2026-06-20", "author": "1"},
    ])

    out = c.get_recent_activity("Acme", days=14)

    c._get_recent_activity_async.assert_awaited_once_with(555, 14)
    assert out == [{"type": "note", "summary": "x", "date": "2026-06-20", "author": "1"}]
    print("✓ sync wrapper resolves account name to numeric ID")


def test_sync_wrapper_returns_empty_when_name_unresolved():
    """An unknown account name short-circuits to [] without calling the async."""
    c = _make_connector()
    c._accounts_cache = [
        {"id": "1", "hubspot_id": 555, "name": "Acme", "tier": "standard", "owner": ""},
    ]
    c._get_recent_activity_async = AsyncMock(return_value=[{"type": "note"}])

    out = c.get_recent_activity("Nonexistent Co", days=14)

    assert out == []
    c._get_recent_activity_async.assert_not_awaited()
    print("✓ sync wrapper returns [] when name cannot be resolved")


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.run(["python3", "-m", "pytest", __file__, "-v"], cwd=str(REPO_ROOT)).returncode)
