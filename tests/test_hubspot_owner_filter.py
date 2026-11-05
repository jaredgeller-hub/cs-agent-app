"""
tests/test_hubspot_owner_filter.py

F9 — owner-scoped companies search via HubSpot MCP get_user_details.

Covers _resolve_owner_id_via_mcp (caching, success parse, failure WARNING)
and the _get_accounts_async integration. The integration tests pin the
fail-CLOSED contract: there is no code path in _get_accounts_async that
issues an unfiltered companies query. A transient MCP error, an unresolved
owner_id, or a non-OK upstream response must each yield [] and record
SourceStatus.FAILED on the connector, not retry unfiltered.

This contract replaces the prior fail-OPEN behavior (an unfiltered retry
on owner=None or zero owner-scoped rows). In a shared HubSpot portal,
unfiltered fallback returned every colleague's account to the user who
owned none.

Standalone-runnable:
    python3 tests/test_hubspot_owner_filter.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.connectors.hubspot_crm import HubSpotCRMConnector
from product.connectors.source_status import SourceStatus


@pytest.fixture(autouse=True)
def _skip_token_load():
    """Avoid the live token loader the connector __init__ calls."""
    with patch.object(HubSpotCRMConnector, "_load_and_refresh_token", return_value=None):
        yield


def _make_text_block(payload) -> MagicMock:
    block = MagicMock()
    block.text = json.dumps(payload)
    return block


def _connector() -> HubSpotCRMConnector:
    conn = HubSpotCRMConnector("user_test")
    conn._access_token = "fake_token"
    conn._tool_names = {"search_crm_objects": "", "get_user_details": ""}
    return conn


# ── _resolve_owner_id_via_mcp ─────────────────────────────────────────────────

def test_resolve_owner_id_caches():
    """Second call must not invoke _mcp_call_tool again."""
    conn = _connector()
    payload = {"userInformation": {"ownerId": 90345227}}
    conn._mcp_call_tool = AsyncMock(
        return_value=([_make_text_block(payload)], SourceStatus.OK)
    )
    first = asyncio.run(conn._resolve_owner_id_via_mcp())
    second = asyncio.run(conn._resolve_owner_id_via_mcp())
    assert first == 90345227
    assert second == 90345227
    assert conn._mcp_call_tool.await_count == 1


def test_resolve_owner_id_returns_owner():
    """Parses userInformation.ownerId from the real MCP response shape."""
    conn = _connector()
    payload = {
        "accountId": 245766817,
        "userId": 90345227,
        "userInformation": {
            "ownerId": 90345227,
            "email": "owner@example.com",
            "firstName": "Test",
            "lastName": "User",
            "type": "PERSON",
        },
    }
    conn._mcp_call_tool = AsyncMock(
        return_value=([_make_text_block(payload)], SourceStatus.OK)
    )
    owner_id = asyncio.run(conn._resolve_owner_id_via_mcp())
    assert owner_id == 90345227
    conn._mcp_call_tool.assert_awaited_once_with(
        "get_user_details", {"include": ["USER_INFORMATION"]}
    )


def test_resolve_owner_id_failure_logs_warning(caplog):
    """Exception from _mcp_call_tool → method returns None and logs WARNING."""
    conn = _connector()
    conn._mcp_call_tool = AsyncMock(side_effect=RuntimeError("connection refused"))
    with caplog.at_level(logging.WARNING, logger="cs-agent.hubspot_crm"):
        result = asyncio.run(conn._resolve_owner_id_via_mcp())
    assert result is None
    assert any(
        "_resolve_owner_id_via_mcp" in r.getMessage() for r in caplog.records
    ), f"expected WARNING about _resolve_owner_id_via_mcp; got: {[r.getMessage() for r in caplog.records]}"


# ── _get_accounts_async fail-closed contract ──────────────────────────────────
#
# Every test in this section is an invariant: there must be no code path
# that calls the companies/search tool without an owner filter. The unit
# under test is exactly the absence of an unfiltered fallback.

def test_get_accounts_filters_by_owner():
    """Owner resolves: search is called once with filterGroups, status OK."""
    conn = _connector()
    conn._resolve_owner_id_via_mcp = AsyncMock(return_value=12345)
    company_payload = [{
        "id": "1",
        "properties": {
            "name": "Acme",
            "domain": "acme.example",
            "industry": "Tech",
            "hubspot_owner_id": "12345",
        },
    }]
    conn._mcp_call_tool = AsyncMock(
        return_value=([_make_text_block(company_payload)], SourceStatus.OK)
    )
    accounts = asyncio.run(conn._get_accounts_async())

    assert len(accounts) == 1
    assert accounts[0]["name"] == "Acme"
    assert conn._accounts_status == SourceStatus.OK
    assert conn.get_accounts_status() == SourceStatus.OK
    assert conn._mcp_call_tool.await_count == 1

    args, _ = conn._mcp_call_tool.call_args
    tool_name, arguments = args[0], args[1]
    assert tool_name == "search_crm_objects"
    assert arguments["objectType"] == "companies"
    assert arguments["limit"] == 100
    assert arguments["filterGroups"] == [{
        "filters": [{
            "propertyName": "hubspot_owner_id",
            "operator": "EQ",
            "value": "12345",
        }]
    }]


def test_owner_resolution_failure_returns_failed_no_company_call(caplog):
    """Owner=None → no company-tool call at all, [] with status FAILED.

    Replaces the prior fail-open contract that, on owner=None, issued an
    unfiltered companies query. That path leaked the entire shared portal
    in production and is now forbidden.
    """
    conn = _connector()
    conn._resolve_owner_id_via_mcp = AsyncMock(return_value=None)
    conn._mcp_call_tool = AsyncMock(
        return_value=([_make_text_block([])], SourceStatus.OK)
    )

    with caplog.at_level(logging.WARNING, logger="cs-agent.hubspot_crm"):
        accounts = asyncio.run(conn._get_accounts_async())

    assert accounts == []
    assert conn._accounts_status == SourceStatus.FAILED
    assert conn.get_accounts_status() == SourceStatus.FAILED

    # No companies call was issued. _resolve_owner_id_via_mcp is mocked, so
    # _mcp_call_tool would only fire for the companies query — must be 0.
    assert conn._mcp_call_tool.await_count == 0

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "fail-closed" in m and "Owner resolution failed" in m for m in messages
    ), f"expected fail-closed owner-resolution WARNING; got: {messages}"
    # The old fail-open log MUST NOT fire any more.
    assert not any(
        "returning unfiltered companies" in m for m in messages
    ), f"legacy fail-open log must not fire; got: {messages}"


def test_owner_filter_zero_matches_returns_empty_no_retry(caplog):
    """Owner OK, filtered call returns OK + 0 items → status EMPTY, no retry.

    Legitimate "this CSM owns nothing" — surface as EMPTY so the caller
    can render an explicit empty state instead of an outage. There must
    be no unfiltered retry; that's what leaked the portal previously.
    """
    conn = _connector()
    conn._resolve_owner_id_via_mcp = AsyncMock(return_value=999)
    conn._mcp_call_tool = AsyncMock(
        return_value=([_make_text_block([])], SourceStatus.OK)
    )

    with caplog.at_level(logging.WARNING, logger="cs-agent.hubspot_crm"):
        accounts = asyncio.run(conn._get_accounts_async())

    assert accounts == []
    assert conn._accounts_status == SourceStatus.EMPTY
    assert conn.get_accounts_status() == SourceStatus.EMPTY
    assert conn._mcp_call_tool.await_count == 1

    # The only call was the filtered one.
    args, _ = conn._mcp_call_tool.call_args
    arguments = args[1]
    assert "filterGroups" in arguments
    assert arguments["filterGroups"][0]["filters"][0]["value"] == "999"

    # No retry log of any prior shape should fire.
    messages = [r.getMessage() for r in caplog.records]
    assert not any(
        "retrying unfiltered" in m for m in messages
    ), f"unfiltered retry must never fire; got: {messages}"


def test_owner_filter_with_matches_does_not_retry(caplog):
    """Owner resolves, filtered call returns OK + N items → single call, status OK."""
    conn = _connector()
    conn._resolve_owner_id_via_mcp = AsyncMock(return_value=999)
    company_payload = [
        {"id": "1", "properties": {"name": "Acme", "hubspot_owner_id": "999"}},
        {"id": "2", "properties": {"name": "Beacon", "hubspot_owner_id": "999"}},
    ]
    conn._mcp_call_tool = AsyncMock(
        return_value=([_make_text_block(company_payload)], SourceStatus.OK)
    )

    with caplog.at_level(logging.WARNING, logger="cs-agent.hubspot_crm"):
        accounts = asyncio.run(conn._get_accounts_async())

    assert conn._mcp_call_tool.await_count == 1
    assert len(accounts) == 2
    assert {a["name"] for a in accounts} == {"Acme", "Beacon"}
    assert conn._accounts_status == SourceStatus.OK
    assert conn.get_accounts_status() == SourceStatus.OK
    assert not any(
        "retrying unfiltered" in r.getMessage() for r in caplog.records
    ), f"unexpected retry WARNING: {[r.getMessage() for r in caplog.records]}"


def test_owner_filter_non_ok_status_returns_failed_no_retry(caplog):
    """Filtered call returns non-OK (FAILED) → [] + status FAILED, no retry.

    This is the exact regression the Stage-1 audit flagged: previously the
    code retried unfiltered on a transient non-OK response from the
    filtered call, which leaked the entire portal to users who happened to
    own none of it. The retry path is gone.
    """
    conn = _connector()
    conn._resolve_owner_id_via_mcp = AsyncMock(return_value=999)
    conn._mcp_call_tool = AsyncMock(
        return_value=([], SourceStatus.FAILED),
    )

    with caplog.at_level(logging.WARNING, logger="cs-agent.hubspot_crm"):
        accounts = asyncio.run(conn._get_accounts_async())

    assert accounts == []
    assert conn._accounts_status == SourceStatus.FAILED
    assert conn.get_accounts_status() == SourceStatus.FAILED
    # Exactly one call (the filtered one). No unfiltered retry.
    assert conn._mcp_call_tool.await_count == 1

    args, _ = conn._mcp_call_tool.call_args
    arguments = args[1]
    assert "filterGroups" in arguments, "the only call must carry filterGroups"

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "fail-closed" in m and "Owner-filtered company query failed" in m
        for m in messages
    ), f"expected fail-closed WARNING; got: {messages}"
    assert not any(
        "retrying unfiltered" in m for m in messages
    ), f"unfiltered retry must never fire; got: {messages}"


def test_no_unfiltered_call_ever_issued_under_any_mock_combination():
    """Sweep: across every {owner_id, status, items} combination, no MCP call lacks filterGroups."""
    cases = [
        # (owner_id, status, items)
        (None,  SourceStatus.OK,     []),
        (None,  SourceStatus.OK,     [{"id": "1", "properties": {"name": "X"}}]),
        (None,  SourceStatus.FAILED, []),
        (999,   SourceStatus.OK,     []),
        (999,   SourceStatus.OK,     [{"id": "1", "properties": {"name": "X"}}]),
        (999,   SourceStatus.FAILED, []),
        (999,   SourceStatus.EMPTY,  []),
    ]
    for owner_id, status, items in cases:
        conn = _connector()
        conn._resolve_owner_id_via_mcp = AsyncMock(return_value=owner_id)
        conn._mcp_call_tool = AsyncMock(
            return_value=([_make_text_block(items)], status)
        )
        asyncio.run(conn._get_accounts_async())
        for call in conn._mcp_call_tool.call_args_list:
            args, _ = call
            arguments = args[1]
            assert "filterGroups" in arguments, (
                f"unfiltered companies call detected for case owner_id={owner_id}, "
                f"status={status}, items={items}; args={arguments}"
            )


# ── _accounts_cache pinning rules ─────────────────────────────────────────────

def test_failed_status_does_not_pin_accounts_cache():
    """A FAILED fetch must not pin [] into _accounts_cache; next call retries.

    Bug scenario: a transient HubSpot 429 returns FAILED. If get_accounts()
    cached []  as if OK, every subsequent call this process serves the wrong
    answer until the worker restarts. The cache must only hold OK / EMPTY
    results.
    """
    conn = _connector()
    # Ensure the sync path runs the async method (not the mock short-circuit).
    conn._mock = False
    conn._access_token = "fake_token"

    # 1) First call: owner-filtered query fails. Expect [] + FAILED + no cache.
    async def _fail_path():
        conn._accounts_status = SourceStatus.FAILED
        return []
    with patch.object(HubSpotCRMConnector, "_get_accounts_async", new=AsyncMock(side_effect=_fail_path)):
        result1 = conn.get_accounts()
    assert result1 == []
    assert conn.get_accounts_status() == SourceStatus.FAILED
    assert conn._accounts_cache is None, (
        "FAILED result must not pin _accounts_cache; got "
        f"{conn._accounts_cache!r}"
    )

    # 2) Second call: the upstream recovered. Cache hydrates with real rows.
    async def _ok_path():
        conn._accounts_status = SourceStatus.OK
        return [{"id": "1", "hubspot_id": 1, "name": "Acme", "owner_id": "999"}]
    with patch.object(HubSpotCRMConnector, "_get_accounts_async", new=AsyncMock(side_effect=_ok_path)):
        result2 = conn.get_accounts()
    assert len(result2) == 1
    assert result2[0]["name"] == "Acme"
    assert conn.get_accounts_status() == SourceStatus.OK
    assert conn._accounts_cache is not None
    assert conn._accounts_cache[0]["name"] == "Acme"


def test_ok_status_pins_accounts_cache():
    """OK fetch pins _accounts_cache; a second call returns the cached list without re-hitting MCP."""
    conn = _connector()
    conn._mock = False
    conn._access_token = "fake_token"

    call_count = {"n": 0}
    async def _ok_path():
        call_count["n"] += 1
        conn._accounts_status = SourceStatus.OK
        return [{"id": "7", "hubspot_id": 7, "name": "Northgate", "owner_id": "999"}]

    with patch.object(HubSpotCRMConnector, "_get_accounts_async", new=AsyncMock(side_effect=_ok_path)):
        first = conn.get_accounts()
        second = conn.get_accounts()

    assert len(first) == 1 and first[0]["name"] == "Northgate"
    assert second is first or second == first
    assert call_count["n"] == 1, "second call must serve from cache, not re-hit MCP"
    assert conn.get_accounts_status() == SourceStatus.OK


# ── Status accessor default ───────────────────────────────────────────────────

def test_get_accounts_status_defaults_to_failed_before_any_fetch():
    """A fresh connector reports FAILED until the first fetch runs.

    The accessor must not return OK by default — callers that read it
    without first calling get_accounts() would otherwise treat absence of
    data as legitimate empty.
    """
    conn = _connector()
    assert conn.get_accounts_status() == SourceStatus.FAILED


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
