"""
tests/test_chat_tool_resolve_contact.py

Unit tests for the resolve_contact_email tool implementation in
product/chat_tools/registry.py.

Mocks the HubSpot token loader and HubSpotCRMConnector so no live MCP
calls happen. Covers four cases: scoped lookup with one match, unscoped
lookup that walks the accounts list, no match, and missing token.

Standalone-runnable:
    python3 tests/test_chat_tool_resolve_contact.py
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.chat_tools.registry import dispatch_tool
from product.connectors.source_status import SourceStatus


def _wrap(items):
    """Wrap a list as the (items, SourceStatus) tuple the connector now returns."""
    items = items or []
    return items, SourceStatus.OK if items else SourceStatus.EMPTY


def _make_connector(get_contacts_return=None, accounts_async_return=None, contacts_by_id_async_return=None):
    """Build a HubSpotCRMConnector-shaped MagicMock with the methods the
    tool calls. The batch() async context manager yields the connector."""
    connector = MagicMock(name="HubSpotCRMConnector")
    connector.get_contacts = MagicMock(return_value=get_contacts_return or [])
    connector._get_accounts_async = AsyncMock(return_value=accounts_async_return or [])
    connector._get_contacts_by_id_async = AsyncMock(return_value=_wrap(contacts_by_id_async_return))

    @asynccontextmanager
    async def _batch():
        yield connector

    connector.batch = _batch
    return connector


def test_resolve_contact_email_scoped_single_match():
    """account_name supplied; one matching contact returned via get_contacts."""
    connector = _make_connector(
        get_contacts_return=[
            {"name": "Marcus Reyes", "email": "marcus@m.example", "role": "VP", "account_name": "Meridian Health"},
        ],
    )

    with patch(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        return_value={"access_token": "fake_token", "expires_at": 9999999999},
    ), patch(
        "product.connectors.hubspot_crm.HubSpotCRMConnector",
        return_value=connector,
    ):
        result = asyncio.run(dispatch_tool(
            "resolve_contact_email",
            {"name": "Marcus", "account_name": "Meridian Health"},
            "user_x",
        ))

    assert "error" not in result
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["name"] == "Marcus Reyes"
    assert match["email"] == "marcus@m.example"
    assert match["account_name"] == "Meridian Health"
    connector.get_contacts.assert_called_once_with("Meridian Health")


def test_resolve_contact_email_unscoped_walks_accounts():
    """account_name omitted; tool walks the accounts list via batch session."""
    connector = _make_connector(
        accounts_async_return=[
            {"id": "123", "hubspot_id": 123, "name": "Meridian Health"},
        ],
        contacts_by_id_async_return=[
            {"name": "Marcus Reyes", "email": "marcus@m.example", "role": "VP"},
        ],
    )

    with patch(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        return_value={"access_token": "fake_token", "expires_at": 9999999999},
    ), patch(
        "product.connectors.hubspot_crm.HubSpotCRMConnector",
        return_value=connector,
    ):
        result = asyncio.run(dispatch_tool(
            "resolve_contact_email",
            {"name": "Marcus"},
            "user_x",
        ))

    assert "error" not in result
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["name"] == "Marcus Reyes"
    assert match["email"] == "marcus@m.example"
    assert match["account_name"] == "Meridian Health"
    connector._get_accounts_async.assert_awaited_once()
    connector._get_contacts_by_id_async.assert_awaited_once_with(123)


def test_resolve_contact_email_no_match():
    """Connector returns contacts but none match the query name."""
    connector = _make_connector(
        get_contacts_return=[
            {"name": "Priya Sharma", "email": "priya@b.example", "role": "Director", "account_name": "Beacon"},
        ],
    )

    with patch(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        return_value={"access_token": "fake_token", "expires_at": 9999999999},
    ), patch(
        "product.connectors.hubspot_crm.HubSpotCRMConnector",
        return_value=connector,
    ):
        result = asyncio.run(dispatch_tool(
            "resolve_contact_email",
            {"name": "Marcus", "account_name": "Beacon"},
            "user_x",
        ))

    assert result == {"matches": []}


def test_resolve_contact_email_no_token():
    """Token loader returns None; tool returns hubspot_not_connected error."""
    with patch(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        return_value=None,
    ):
        result = asyncio.run(dispatch_tool(
            "resolve_contact_email",
            {"name": "Marcus", "account_name": "Meridian Health"},
            "user_x",
        ))

    assert result == {"matches": [], "error": "hubspot_not_connected"}


def main_runner():
    """Standalone runner."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
