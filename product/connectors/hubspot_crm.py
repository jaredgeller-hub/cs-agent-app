"""
product/connectors/hubspot_crm.py

HubSpot CRM connector using the HubSpot Remote MCP Server (mcp.hubspot.com).
Authenticates via the OAuth 2.1 token managed in hubspot_mcp.py.

Mock mode: set HUBSPOT_USE_MOCK=true to return realistic demo data without
connecting to the MCP server (useful for local dev and CI).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta

import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from product.connectors.base import CRMConnector
from product.connectors.hubspot_mcp import (
    load_hubspot_token,
    refresh_hubspot_token,
    save_hubspot_token,
)
from product.connectors.source_status import SourceStatus

logger = logging.getLogger("cs-agent.hubspot_crm")

_MCP_URL = "https://mcp.hubspot.com/"


# ── Circuit breaker ───────────────────────────────────────────────────────────
_circuit_breaker_until: float = 0.0  # timestamp when cooldown expires
_consecutive_429s: int = 0
_CIRCUIT_BREAKER_THRESHOLD = 3       # consecutive 429s before tripping
_CIRCUIT_BREAKER_COOLDOWN = 300      # seconds to pause (5 minutes)


def _check_circuit_breaker() -> bool:
    """Return True if MCP calls should be blocked (circuit is open)."""
    if time.time() < _circuit_breaker_until:
        return True
    return False


def _record_429():
    """Record a 429 response. Trips the breaker after threshold consecutive 429s."""
    global _consecutive_429s, _circuit_breaker_until
    _consecutive_429s += 1
    if _consecutive_429s >= _CIRCUIT_BREAKER_THRESHOLD:
        _circuit_breaker_until = time.time() + _CIRCUIT_BREAKER_COOLDOWN
        logger.warning("[hubspot_crm] Circuit breaker OPEN — pausing all MCP calls for %ds after %d consecutive 429s", _CIRCUIT_BREAKER_COOLDOWN, _consecutive_429s)
        _consecutive_429s = 0


def _record_success():
    """Record a successful MCP call. Resets the consecutive 429 counter."""
    global _consecutive_429s
    _consecutive_429s = 0


# ── Mock data ──────────────────────────────────────────────────────────────────

_TODAY = date.today()

_MOCK_ACCOUNTS = [
    {"id": "mock-001", "name": "Meridian Health",       "domain": "meridianhealth.example", "industry": "Healthcare",      "owner_id": "sarah_c"},
    {"id": "mock-002", "name": "Beacon Financial",      "domain": "beaconfinancial.example", "industry": "Financial Services", "owner_id": "sarah_c"},
    {"id": "mock-003", "name": "Sutton Retail",         "domain": "suttonretail.example", "industry": "Retail",          "owner_id": "sarah_c"},
    {"id": "mock-004", "name": "Northgate Logistics",   "domain": "northgate.example",    "industry": "Logistics",       "owner_id": "sarah_c"},
    {"id": "mock-005", "name": "Pinnacle Group",        "domain": "pinnaclegroup.example", "industry": "Technology",      "owner_id": "sarah_c"},
]

_MOCK_HEALTH = {
    "Meridian Health":     {"score": 88, "trend": "stable",    "last_updated": str(_TODAY)},
    "Beacon Financial":    {"score": 74, "trend": "declining",  "last_updated": str(_TODAY)},
    "Sutton Retail":       {"score": 85, "trend": "stable",    "last_updated": str(_TODAY)},
    "Northgate Logistics": {"score": 65, "trend": "declining",  "last_updated": str(_TODAY)},
    "Pinnacle Group":      {"score": 91, "trend": "improving",  "last_updated": str(_TODAY)},
}

_MOCK_RENEWALS = {
    "Meridian Health":     {"renewal_date": str(_TODAY + timedelta(days=120)), "contract_value": 85000},
    "Beacon Financial":    {"renewal_date": str(_TODAY + timedelta(days=90)),  "contract_value": 120000},
    "Sutton Retail":       {"renewal_date": str(_TODAY + timedelta(days=200)), "contract_value": 45000},
    "Northgate Logistics": {"renewal_date": str(_TODAY + timedelta(days=47)),  "contract_value": 95000},
    "Pinnacle Group":      {"renewal_date": str(_TODAY + timedelta(days=180)), "contract_value": 60000},
}

_MOCK_CONTACTS = {
    "mock-001": [{"id": "c-001", "name": "Marcus Reyes",  "email": "marcus.reyes@meridianhealth.example",  "title": "VP Operations",  "last_activity": str(_TODAY - timedelta(days=3))}],
    "mock-002": [{"id": "c-002", "name": "Priya Sharma",  "email": "priya.sharma@beaconfinancial.example", "title": "Head of IT",      "last_activity": str(_TODAY - timedelta(days=8))}],
    "mock-003": [{"id": "c-003", "name": "Jordan Kim",    "email": "jordan.kim@suttonretail.example",      "title": "Director CS",     "last_activity": str(_TODAY - timedelta(days=2))}],
    "mock-004": [{"id": "c-004", "name": "Dana Mills",    "email": "dana.mills@northgate.example",         "title": "COO",             "last_activity": str(_TODAY - timedelta(days=14))}],
    "mock-005": [{"id": "c-005", "name": "Alex Wong",     "email": "alex.wong@pinnaclegroup.example",      "title": "CTO",             "last_activity": str(_TODAY - timedelta(days=1))}],
}

_MOCK_CTAS = {
    "mock-004": [
        {"id": "t-001", "title": "QBR follow-up email",         "status": "open", "due_date": str(_TODAY - timedelta(days=5)), "priority": "high"},
        {"id": "t-002", "title": "Send updated contract terms",  "status": "open", "due_date": str(_TODAY + timedelta(days=2)), "priority": "medium"},
    ],
    "mock-002": [
        {"id": "t-003", "title": "Renewal discovery call",       "status": "open", "due_date": str(_TODAY + timedelta(days=7)), "priority": "high"},
    ],
}

_MOCK_DEALS = {
    "mock-001": [{"name": "Meridian Health - Renewal", "stage": "Contract Sent", "amount": 85000, "close_date": str(_TODAY + timedelta(days=120))}],
    "mock-002": [
        {"name": "Beacon Financial - Renewal",   "stage": "Negotiation",    "amount": 120000, "close_date": str(_TODAY + timedelta(days=90))},
        {"name": "Beacon Financial - Expansion", "stage": "Proposal",       "amount": 40000,  "close_date": str(_TODAY + timedelta(days=30))},
    ],
    "mock-003": [{"name": "Sutton Retail - Renewal",       "stage": "Discovery",      "amount": 45000,  "close_date": str(_TODAY + timedelta(days=200))}],
    "mock-004": [{"name": "Northgate Logistics - Renewal", "stage": "Decision",       "amount": 95000,  "close_date": str(_TODAY + timedelta(days=47))}],
    "mock-005": [{"name": "Pinnacle Group - Renewal",      "stage": "Contract Sent",  "amount": 60000,  "close_date": str(_TODAY + timedelta(days=180))}],
}

_MOCK_TICKETS = {
    "mock-004": [
        {"subject": "QBR follow-up email",        "status": "OPEN", "priority": "HIGH"},
        {"subject": "Send updated contract terms", "status": "OPEN", "priority": "MEDIUM"},
    ],
    "mock-002": [
        {"subject": "Renewal discovery call",      "status": "OPEN", "priority": "HIGH"},
    ],
}

_MOCK_ACTIVITIES = {
    "mock-001": [
        {"type": "meeting",  "timestamp": str(_TODAY - timedelta(days=3)),  "summary": "Monthly check-in — discussed new feature adoption"},
        {"type": "email",    "timestamp": str(_TODAY - timedelta(days=7)),  "summary": "Marcus confirmed Q2 goals alignment"},
    ],
    "mock-002": [
        {"type": "email",    "timestamp": str(_TODAY - timedelta(days=8)),  "summary": "Priya flagged performance concerns with reporting module"},
    ],
    "mock-003": [
        {"type": "meeting",  "timestamp": str(_TODAY - timedelta(days=2)),  "summary": "Onboarding review — 3 new users added"},
    ],
    "mock-004": [],  # intentionally empty — no recent activity
    "mock-005": [
        {"type": "note",     "timestamp": str(_TODAY - timedelta(days=1)),  "summary": "Alex confirmed executive sponsor buy-in for expansion"},
    ],
}


# ── Async/sync bridge ─────────────────────────────────────────────────────────

def _run(coro):
    """Run an async coroutine safely from synchronous code.

    Uses a dedicated thread with its own event loop so this works even when
    called from within a running async context (e.g. FastAPI/uvicorn).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


# ── Connector ─────────────────────────────────────────────────────────────────

class HubSpotCRMConnector(CRMConnector):
    """CRM connector that reads HubSpot data via the Remote MCP Server."""

    def __init__(self, csm_id: str):
        super().__init__(csm_id, {})
        self._mock = os.environ.get("HUBSPOT_USE_MOCK", "").lower() in ("1", "true", "yes")
        self._access_token: str | None = None
        self._tool_names: dict[str, str] | None = None  # name -> description cache
        self._accounts_cache: list[dict] | None = None   # cached get_accounts() result
        # Status of the most recent accounts fetch. Defaults to FAILED so a
        # caller that reads get_accounts_status() before any fetch has run
        # treats absence of data as an unverified outage, not legitimate empty.
        self._accounts_status: SourceStatus = SourceStatus.FAILED
        self._batch_session: ClientSession | None = None
        self._owner_id: int | None = None                # resolved via get_user_details (F9)

        if not self._mock:
            self._load_and_refresh_token()

    def _load_and_refresh_token(self) -> None:
        token = load_hubspot_token(self.csm_id)
        if token is None:
            logger.warning("[hubspot_crm] No HubSpot token found for %s — MCP calls will fail", self.csm_id)
            return
        if time.time() >= token.get("expires_at", 0) - 60:
            from product.token_lock import token_lock
            with token_lock(self.csm_id, "hubspot"):
                # Double-check with the SAME 60s threshold
                token = load_hubspot_token(self.csm_id)
                if token and time.time() >= token.get("expires_at", 0) - 60:
                    logger.debug("[hubspot_crm] Token expired for %s, refreshing", self.csm_id)
                    try:
                        token = refresh_hubspot_token(token["refresh_token"])
                        save_hubspot_token(self.csm_id, token)
                    except Exception as exc:
                        logger.warning("[hubspot_crm] Token refresh failed for %s: %s", self.csm_id, exc)
                        return
        self._access_token = token["access_token"]

    @asynccontextmanager
    async def batch(self):
        """Open a reusable MCP session for multiple tool calls.

        All _mcp_call_tool calls within this block reuse the same TCP connection.
        Outside of this block, each call opens its own session (backward compat).

        Usage:
            async with connector.batch():
                accounts = await connector._get_accounts_async()
                for a in accounts:
                    contacts = await connector._get_contacts_by_id_async(a['hubspot_id'])
        """
        # Pre-refresh token with 5-min buffer to avoid mid-batch 401s
        token = load_hubspot_token(self.csm_id)
        if token and time.time() >= token.get("expires_at", 0) - 300:
            from product.token_lock import token_lock_async
            async with token_lock_async(self.csm_id, "hubspot"):
                # Double-check with the SAME 300s threshold
                token = load_hubspot_token(self.csm_id)
                if token and time.time() >= token.get("expires_at", 0) - 300:
                    try:
                        token = refresh_hubspot_token(token["refresh_token"])
                        save_hubspot_token(self.csm_id, token)
                        self._access_token = token["access_token"]
                    except Exception as exc:
                        logger.warning("[hubspot_crm] Pre-batch token refresh failed: %s", exc)
                else:
                    # Another caller refreshed — adopt their token
                    if token:
                        self._access_token = token["access_token"]

        if not self._access_token:
            logger.warning("[hubspot_crm] No access token for batch — yielding without session")
            yield self
            return

        if _check_circuit_breaker():
            logger.warning("[hubspot_crm] Circuit breaker OPEN — yielding without session")
            yield self
            return

        async with httpx.AsyncClient(
            headers=self._mcp_headers(),
            timeout=30.0,
        ) as http_client:
            async with streamable_http_client(_MCP_URL, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._batch_session = session
                    # Pre-warm tool discovery
                    try:
                        result = await session.list_tools()
                        self._tool_names = {t.name: (t.description or "") for t in result.tools}
                        logger.info("[hubspot_crm] Batch session opened — %d tools discovered", len(self._tool_names))
                    except Exception as exc:
                        logger.warning("[hubspot_crm] Tool discovery in batch failed: %s", exc)
                        self._tool_names = {}
                    try:
                        yield self
                    finally:
                        self._batch_session = None
                        logger.info("[hubspot_crm] Batch session closed")

    # ── MCP client ────────────────────────────────────────────────────────────

    def _mcp_headers(self) -> dict:
        """Build the auth headers required by HubSpot's Remote MCP server."""
        headers = {"Authorization": f"Bearer {self._access_token}"}
        client_id = os.environ.get("HUBSPOT_CLIENT_ID")
        if client_id:
            # HubSpot MCP Inspector docs require both the Bearer token and Client ID
            headers["x-hubspot-client-id"] = client_id
        return headers

    async def _mcp_call_tool(
        self, tool_name: str, arguments: dict
    ) -> tuple[list, SourceStatus]:
        """Call a specific MCP tool. Returns (content, status).

        Handles 401 (token refresh + retry) and 429 (back-off + retry up to 2x).
        A 0.2 s inter-call delay is added after each successful response to avoid
        hammering HubSpot's rate limit when many calls are made in sequence.

        Returns ([], SourceStatus.FAILED) on every failure mode. On success
        returns (content_list, SourceStatus.OK); content may parse to an
        empty list, but distinguishing OK from EMPTY requires parsing and
        is the caller's job.
        """
        if not self._access_token:
            logger.warning("[hubspot_crm] No access token, skipping MCP call to %s", tool_name)
            return [], SourceStatus.FAILED

        if _check_circuit_breaker():
            logger.warning("[hubspot_crm] Circuit breaker OPEN, skipping MCP call to %s", tool_name)
            return [], SourceStatus.FAILED

        # Batch mode: reuse existing session (no new TCP connection)
        if self._batch_session:
            try:
                result = await self._batch_session.call_tool(tool_name, arguments=arguments)
                if result.isError:
                    logger.warning("[hubspot_crm] tool %s returned isError=True (batch): %s", tool_name, result.content)
                    return [], SourceStatus.FAILED
                logger.debug("[hubspot_crm] tool %s returned %d content blocks (batch)", tool_name, len(result.content))
                await asyncio.sleep(0.2)
                _record_success()
                return result.content, SourceStatus.OK
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    _record_429()
                    logger.warning("[hubspot_crm] 429 in batch on %s, waiting 2s", tool_name)
                    await asyncio.sleep(2.0)
                    try:
                        result = await self._batch_session.call_tool(tool_name, arguments=arguments)
                        if not result.isError:
                            await asyncio.sleep(0.2)
                            _record_success()
                            return result.content, SourceStatus.OK
                    except Exception:
                        pass
                logger.warning("[hubspot_crm] Batch call to %s failed: %s", tool_name, exc)
                return [], SourceStatus.FAILED
            except Exception as exc:
                logger.warning("[hubspot_crm] Batch call to %s failed: %s", tool_name, exc)
                return [], SourceStatus.FAILED

        for attempt in range(3):
            try:
                logger.debug("[hubspot_crm] call_tool %s args=%s (attempt %d)", tool_name, arguments, attempt + 1)
                async with httpx.AsyncClient(
                    headers=self._mcp_headers(),
                    timeout=30.0,
                ) as http_client:
                    async with streamable_http_client(_MCP_URL, http_client=http_client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(tool_name, arguments=arguments)
                            if result.isError:
                                logger.warning("[hubspot_crm] tool %s returned isError=True: %s", tool_name, result.content)
                                return [], SourceStatus.FAILED
                            logger.debug("[hubspot_crm] tool %s returned %d content blocks", tool_name, len(result.content))
                            # Small inter-call delay to stay within HubSpot rate limits
                            await asyncio.sleep(0.2)
                            _record_success()
                            return result.content, SourceStatus.OK
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 401 and attempt == 0:
                    logger.debug("[hubspot_crm] 401 on attempt 1, refreshing token and retrying")
                    # Capture the token that just 401'd, used for double-check
                    stale_token = self._access_token
                    from product.token_lock import token_lock_async
                    async with token_lock_async(self.csm_id, "hubspot"):
                        fresh = load_hubspot_token(self.csm_id)
                        if fresh and fresh.get("access_token") != stale_token:
                            # Someone refreshed after our 401, use theirs
                            self._access_token = fresh["access_token"]
                        elif fresh and fresh.get("refresh_token"):
                            try:
                                token = refresh_hubspot_token(fresh["refresh_token"])
                                save_hubspot_token(self.csm_id, token)
                                self._access_token = token["access_token"]
                            except Exception as refresh_exc:
                                logger.warning("[hubspot_crm] Token refresh on 401 failed: %s", refresh_exc)
                                return [], SourceStatus.FAILED
                    continue
                if status == 429 and attempt < 2:
                    _record_429()
                    wait = 1.0 * (attempt + 1)  # 1 s, then 2 s
                    logger.warning("[hubspot_crm] 429 rate limit on %s (attempt %d), retrying in %.1fs", tool_name, attempt + 1, wait)
                    await asyncio.sleep(wait)
                    continue
                logger.warning("[hubspot_crm] HTTP error calling %s: %s", tool_name, exc)
                return [], SourceStatus.FAILED
            except Exception as exc:
                logger.warning("[hubspot_crm] Error calling MCP tool %s: %s", tool_name, exc)
                return [], SourceStatus.FAILED
        return [], SourceStatus.FAILED

    async def _list_tools(self) -> dict[str, str]:
        """Discover available tools from the MCP server. Cached after first call."""
        if self._batch_session and self._tool_names is not None:
            return self._tool_names
        if self._tool_names is not None:
            return self._tool_names

        try:
            async with httpx.AsyncClient(
                headers=self._mcp_headers(),
                timeout=30.0,
            ) as http_client:
                async with streamable_http_client(_MCP_URL, http_client=http_client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        self._tool_names = {t.name: (t.description or "") for t in result.tools}
                        logger.debug("[hubspot_crm] Discovered %d tools: %s", len(self._tool_names), list(self._tool_names.keys()))
                        return self._tool_names
        except Exception as exc:
            logger.warning("[hubspot_crm] _list_tools failed for %s: %s", self.csm_id, exc)
            self._tool_names = {}
            return {}

    def _find_tool(self, *keywords: str) -> str | None:
        """Return the first discovered tool name whose name contains any keyword."""
        if self._tool_names is None:
            return None
        for name in self._tool_names:
            for kw in keywords:
                if kw.lower() in name.lower():
                    return name
        return None

    @staticmethod
    def _text_from_content(content: list) -> str:
        """Extract concatenated text from a CallToolResult content list."""
        parts = []
        for block in content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                parts.append(str(block.data))
        return "\n".join(parts)

    @staticmethod
    def _parse_json_response(content: list) -> list | dict | None:
        """Try to parse the first text content block as JSON."""
        text = HubSpotCRMConnector._text_from_content(content)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text  # caller handles raw text fallback

    # ── Async implementations ─────────────────────────────────────────────────

    async def _resolve_owner_id_via_mcp(self) -> int | None:
        """Resolve the authenticated user's HubSpot owner_id via get_user_details.

        Called once per connector instance; result is cached on self._owner_id.
        Returns None on any failure. _get_accounts_async treats a None return
        as a fail-closed signal and refuses to query companies unfiltered.
        get_user_details is in every MCP scope, so failure here is rare and
        should be visible via WARNING logs.
        """
        if self._owner_id is not None:
            return self._owner_id
        try:
            content, status = await self._mcp_call_tool(
                "get_user_details", {"include": ["USER_INFORMATION"]}
            )
            if status != SourceStatus.OK:
                logger.warning(
                    "[hubspot_crm] _resolve_owner_id_via_mcp non-ok status for %s: %s",
                    self.csm_id, status,
                )
                return None
            data = self._parse_json_response(content)
            if not isinstance(data, dict):
                logger.warning(
                    "[hubspot_crm] _resolve_owner_id_via_mcp non-dict response for %s: %s",
                    self.csm_id, type(data).__name__,
                )
                return None
            user_info = data.get("userInformation") or {}
            owner_id = user_info.get("ownerId")
            if owner_id is None:
                logger.warning(
                    "[hubspot_crm] _resolve_owner_id_via_mcp missing userInformation.ownerId for %s: top_keys=%s",
                    self.csm_id, list(data.keys()),
                )
                return None
            self._owner_id = int(owner_id)
            return self._owner_id
        except Exception as exc:
            logger.warning(
                "[hubspot_crm] _resolve_owner_id_via_mcp failed for %s: %s",
                self.csm_id, exc,
            )
            return None

    async def _get_accounts_async(self) -> list[dict]:
        """Fetch the authenticated user's owned companies. Fail-closed.

        Every non-owner-scoped path returns [] and records FAILED — there
        is no code path here that issues an unfiltered companies query.
        In a shared HubSpot portal an unfiltered fallback would leak
        colleagues' accounts to the user, so a transient MCP error, an
        unresolved owner_id, or an open circuit breaker must surface as a
        data-issue (FAILED) rather than degrade silently to "the whole
        portal" or to a misleading empty list.

        Status semantics, set on self._accounts_status before returning:
          OK     — owner resolved, scoped query succeeded with rows.
          EMPTY  — owner resolved, scoped query succeeded with zero rows
                   (legitimate: the user owns nothing in the portal).
          FAILED — owner could not be resolved, the scoped query returned
                   non-OK, no company tool was discovered, or the response
                   could not be parsed.
        """
        await self._list_tools()
        tool = self._find_tool("compan", "account", "list_crm", "search_crm")
        if not tool:
            logger.warning(
                "[hubspot_crm] No company/account tool found for %s — returning [] (fail-closed)",
                self.csm_id,
            )
            self._accounts_status = SourceStatus.FAILED
            return []

        owner_id = await self._resolve_owner_id_via_mcp()
        if owner_id is None:
            # Fail-closed: refuse to query companies without an owner filter.
            # In a shared portal the unfiltered set is every colleague's
            # account; surfacing FAILED lets the caller render a data-issue
            # state instead of a confidentiality leak or a misleading empty.
            logger.warning(
                "[hubspot_crm] Owner resolution failed for %s — returning [] (fail-closed, no unfiltered fallback)",
                self.csm_id,
            )
            self._accounts_status = SourceStatus.FAILED
            return []

        arguments: dict = {
            "objectType": "companies",
            "limit": 100,
            "filterGroups": [{
                "filters": [{
                    "propertyName": "hubspot_owner_id",
                    "operator": "EQ",
                    "value": str(owner_id),
                }],
            }],
        }
        content, status = await self._mcp_call_tool(tool, arguments)
        if status != SourceStatus.OK:
            # Transient error, 429, circuit breaker, or upstream tool error.
            # Do NOT retry unfiltered — the previous fail-open behavior here
            # leaked the entire portal to users who own no companies. The
            # caller reads get_accounts_status() and surfaces a data-issue.
            logger.warning(
                "[hubspot_crm] Owner-filtered company query failed for %s "
                "(resolved_owner_id=%s, status=%s) — returning [] (fail-closed, no unfiltered retry)",
                self.csm_id, owner_id, status,
            )
            self._accounts_status = SourceStatus.FAILED
            return []

        data = self._parse_json_response(content)
        if data is None:
            logger.warning(
                "[hubspot_crm] Owner-filtered company query for %s returned unparseable content "
                "(resolved_owner_id=%s) — returning [] (fail-closed)",
                self.csm_id, owner_id,
            )
            self._accounts_status = SourceStatus.FAILED
            return []
        items = data if isinstance(data, list) else (data.get("results", []) if isinstance(data, dict) else [])
        logger.info(
            "[hubspot_crm] Owner-scoped company query for %s: resolved_owner_id=%s, rows=%d",
            self.csm_id, owner_id, len(items),
        )

        results = []
        for item in items:
            props = item.get("properties", item)
            raw_id = item.get("id", props.get("hs_object_id", ""))
            try:
                hubspot_id = int(raw_id)
            except (TypeError, ValueError):
                hubspot_id = None
            results.append({
                "id":         str(raw_id),
                "hubspot_id": hubspot_id,
                "name":       props.get("name", props.get("company_name", "")),
                "domain":     props.get("domain", ""),
                "industry":   props.get("industry", ""),
                "owner_id":   props.get("hubspot_owner_id", ""),
            })
        self._accounts_status = SourceStatus.OK if results else SourceStatus.EMPTY
        return results

    async def _get_contacts_by_id_async(
        self, hubspot_id: int
    ) -> tuple[list[dict], SourceStatus]:
        """Fetch contacts associated with a HubSpot company by its numeric ID.

        Returns (items, status) where status distinguishes OK (records returned),
        EMPTY (fetch succeeded with zero records), and FAILED (transport,
        auth, parse, or upstream error).
        """
        await self._list_tools()
        tool = self._find_tool("contact", "search_crm")
        if not tool:
            return [], SourceStatus.FAILED
        content, status = await self._mcp_call_tool(tool, {
            "objectType": "contacts",
            "filterGroups": [{"associatedWith": [{"objectType": "companies", "operator": "EQUAL", "objectIdValues": [hubspot_id]}]}],
            "properties": ["firstname", "lastname", "email", "jobtitle", "phone"],
            "limit": 10,
        })
        if status == SourceStatus.FAILED:
            return [], SourceStatus.FAILED
        data = self._parse_json_response(content)
        if data is None:
            return [], SourceStatus.FAILED
        items = data if isinstance(data, list) else (data.get("results", []) if isinstance(data, dict) else [])
        if not items:
            return [], SourceStatus.EMPTY
        results = []
        for item in items:
            props = item.get("properties", item)
            results.append({
                "name":  f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
                "email": props.get("email", ""),
                "role":  props.get("jobtitle", ""),
            })
        return results, SourceStatus.OK

    async def _get_deals_for_company_async(
        self, hubspot_id: int
    ) -> tuple[list[dict], SourceStatus]:
        """Fetch deals associated with a HubSpot company by its numeric ID.

        Returns (items, status). See _get_contacts_by_id_async for status semantics.
        """
        await self._list_tools()
        tool = self._find_tool("deal", "search_crm")
        if not tool:
            return [], SourceStatus.FAILED
        content, status = await self._mcp_call_tool(tool, {
            "objectType": "deals",
            "filterGroups": [{"associatedWith": [{"objectType": "companies", "operator": "EQUAL", "objectIdValues": [hubspot_id]}]}],
            "properties": ["dealname", "dealstage", "amount", "closedate", "pipeline"],
            "limit": 10,
        })
        if status == SourceStatus.FAILED:
            return [], SourceStatus.FAILED
        data = self._parse_json_response(content)
        if data is None:
            return [], SourceStatus.FAILED
        items = data if isinstance(data, list) else (data.get("results", []) if isinstance(data, dict) else [])
        if not items:
            return [], SourceStatus.EMPTY
        results = []
        for item in items:
            props = item.get("properties", item)
            results.append({
                "name":       props.get("dealname", ""),
                "stage":      props.get("dealstage", ""),
                "amount":     props.get("amount"),
                "close_date": str(props.get("closedate", ""))[:10],
            })
        return results, SourceStatus.OK

    async def _get_tickets_for_company_async(
        self, hubspot_id: int
    ) -> tuple[list[dict], SourceStatus]:
        """Fetch open tickets associated with a HubSpot company by its numeric ID.

        Returns (items, status). See _get_contacts_by_id_async for status semantics.
        """
        await self._list_tools()
        tool = self._find_tool("ticket", "search_crm")
        if not tool:
            return [], SourceStatus.FAILED
        content, status = await self._mcp_call_tool(tool, {
            "objectType": "tickets",
            "filterGroups": [{"associatedWith": [{"objectType": "companies", "operator": "EQUAL", "objectIdValues": [hubspot_id]}]}],
            "properties": ["subject", "content", "hs_pipeline_stage", "hs_ticket_priority", "createdate"],
            "limit": 10,
        })
        if status == SourceStatus.FAILED:
            return [], SourceStatus.FAILED
        data = self._parse_json_response(content)
        if data is None:
            return [], SourceStatus.FAILED
        items = data if isinstance(data, list) else (data.get("results", []) if isinstance(data, dict) else [])
        if not items:
            return [], SourceStatus.EMPTY
        results = []
        for item in items:
            props = item.get("properties", item)
            results.append({
                "subject":  props.get("subject", props.get("hs_ticket_id", "")),
                "status":   props.get("hs_pipeline_stage", "OPEN").upper(),
                "priority": (props.get("hs_ticket_priority") or "MEDIUM").upper(),
            })
        return results, SourceStatus.OK

    async def _get_renewals_async(self) -> list[dict]:
        await self._list_tools()
        tool = self._find_tool("deal", "renewal", "search_crm")
        if not tool:
            logger.warning("[hubspot_crm] No deals tool found")
            return []
        content, _status = await self._mcp_call_tool(tool, {"objectType": "deals", "properties": ["dealname", "closedate", "amount", "dealstage", "associations.companies"], "limit": 100})
        data = self._parse_json_response(content)
        results = []
        today = date.today()
        items = data if isinstance(data, list) else (data.get("results", []) if isinstance(data, dict) else [])
        for item in items:
            props = item.get("properties", item)
            closedate = props.get("closedate", "")
            if not closedate:
                continue
            try:
                renewal_date = date.fromisoformat(str(closedate)[:10])
                days_until = (renewal_date - today).days
                # Carry the associated company ID only. get_renewals() resolves
                # it to the company name (the join key used everywhere else)
                # and drops rows it cannot resolve. Never fall back to dealname:
                # it is record content and does not join to accounts.
                company_id = (
                    item.get("associations", {}).get("companies", {}).get("results", [{}])[0].get("id", "")
                    if item.get("associations") else props.get("associatedcompanyid", "")
                )
                results.append({
                    "company_id":         str(company_id) if company_id else "",
                    "record_id":          str(item.get("id") or props.get("hs_object_id") or ""),
                    "renewal_date":       str(renewal_date),
                    "contract_value":     props.get("amount"),
                    "days_until_renewal": days_until,
                })
            except (ValueError, TypeError, IndexError):
                continue
        return results

    async def _get_health_scores_async(self) -> list[dict]:
        # HubSpot doesn't have a native health score — return empty so the pipeline
        # falls back gracefully. A future phase can map custom HubSpot properties.
        logger.debug("[hubspot_crm] get_health_scores: HubSpot has no native health score; returning empty")
        return []

    async def _get_open_ctas_async(self) -> list[dict]:
        await self._list_tools()
        tool = self._find_tool("ticket", "task", "cta", "search_crm")
        if not tool:
            logger.warning("[hubspot_crm] No tickets/tasks tool found")
            return []
        content, _status = await self._mcp_call_tool(tool, {"objectType": "tickets", "filters": [{"propertyName": "hs_pipeline_stage", "operator": "NEQ", "value": "closed"}], "limit": 100})
        data = self._parse_json_response(content)
        results = []
        today = date.today()
        items = data if isinstance(data, list) else (data.get("results", []) if isinstance(data, dict) else [])
        for item in items:
            props = item.get("properties", item)
            due_raw = props.get("hs_ticket_priority_date", props.get("closedate", ""))
            due_str = str(due_raw)[:10] if due_raw else ""
            status = "upcoming"
            if due_str:
                try:
                    due_date = date.fromisoformat(due_str)
                    if due_date < today:
                        status = "overdue"
                    elif due_date == today:
                        status = "due_today"
                except ValueError:
                    pass
            # Carry the associated company ID only; get_open_ctas() resolves
            # it to the company name and drops rows it cannot resolve.
            company_id = (
                item.get("associations", {}).get("companies", {}).get("results", [{}])[0].get("id", "")
                if item.get("associations") else ""
            )
            results.append({
                "company_id":   str(company_id) if company_id else "",
                "record_id":    str(item.get("id") or props.get("hs_object_id") or ""),
                "title":        props.get("subject", props.get("hs_ticket_id", "")),
                "due_date":     due_str,
                "priority":     props.get("hs_ticket_priority", "medium").lower(),
                "status":       status,
                "owner":        props.get("hubspot_owner_id", ""),
            })
        return results

    async def _get_recent_activity_async(self, hubspot_id: int, days: int) -> list[dict]:
        """Fetch recent notes logged on a company, by numeric HubSpot company ID.

        Queries the `notes` object type with the associatedWith filter — the
        same proven pattern as _get_contacts_by_id_async / deals / tickets —
        so notes written via HubSpotWriter.log_note (which POSTs
        /crm/v3/objects/notes and associates note_to_company) are surfaced
        back to the dossier. Reads hs_note_body, the field log_note writes,
        rather than hs_body_preview. The prior implementation queried
        objectType "engagements" with a name-string `associations` filter
        (violating the numeric-ID association rule) and read hs_body_preview,
        so logged notes were never returned (F-2).

        Returns a bare list (no SourceStatus tuple) to match the dossier
        caller's existing _normalize handling and the sync get_recent_activity
        wrapper consumed by postcall/fallback.
        """
        await self._list_tools()
        tool = self._find_tool("note", "search_crm")
        if not tool:
            logger.warning("[hubspot_crm] No notes/search tool found")
            return []
        content, status = await self._mcp_call_tool(tool, {
            "objectType": "notes",
            "filterGroups": [{"associatedWith": [{"objectType": "companies", "operator": "EQUAL", "objectIdValues": [hubspot_id]}]}],
            "properties": ["hs_note_body", "hs_timestamp", "hs_lastmodifieddate", "hubspot_owner_id"],
            "limit": 50,
        })
        if status == SourceStatus.FAILED:
            return []
        data = self._parse_json_response(content)
        items = data if isinstance(data, list) else (data.get("results", []) if isinstance(data, dict) else [])
        results = []
        cutoff = date.today() - timedelta(days=days)
        for item in items:
            props = item.get("properties", item)
            ts = props.get("hs_timestamp", props.get("hs_lastmodifieddate", props.get("createdate", "")))
            ts_str = str(ts)[:10] if ts else ""
            try:
                if ts_str and date.fromisoformat(ts_str) < cutoff:
                    continue
            except ValueError:
                pass
            results.append({
                "type":    "note",
                "summary": props.get("hs_note_body", "") or "",
                "date":    ts_str,
                "author":  props.get("hubspot_owner_id", ""),
            })
        return results

    # ── ABC interface (sync) ──────────────────────────────────────────────────

    def get_accounts(self) -> list[dict]:
        if self._accounts_cache is not None:
            return self._accounts_cache
        if self._mock:
            self._accounts_cache = [
                {"id": a["id"], "hubspot_id": None, "name": a["name"], "tier": "standard", "owner": self.csm_id}
                for a in _MOCK_ACCOUNTS
            ]
            self._accounts_status = SourceStatus.OK
            return self._accounts_cache
        try:
            raw = _run(self._get_accounts_async())
            # _get_accounts_async sets self._accounts_status on every branch.
            # Refuse to pin a FAILED result into _accounts_cache so a later
            # call retries instead of serving an empty list as if authoritative.
            if self._accounts_status == SourceStatus.FAILED:
                return []
            self._accounts_cache = [
                {"id": a["id"], "hubspot_id": a.get("hubspot_id"), "name": a["name"], "tier": "standard", "owner": a.get("owner_id", "")}
                for a in raw
            ]
            return self._accounts_cache
        except Exception as exc:
            logger.warning("[hubspot_crm] get_accounts failed: %s", exc)
            self._accounts_status = SourceStatus.FAILED
            return []

    def get_accounts_status(self) -> SourceStatus:
        """Return the status from the most recent get_accounts() fetch.

        OK     — accounts returned (mock mode also returns OK).
        EMPTY  — owner resolved and query succeeded with zero rows; the
                 user legitimately owns no companies in the connected portal.
        FAILED — owner could not be resolved, the MCP call returned non-OK,
                 an exception was raised, or get_accounts() has not yet run.
                 Callers must render this as a data-issue, not as "no accounts".
        """
        return self._accounts_status

    def _resolve_company_id(self, account_name: str) -> int | None:
        """Resolve an account name to its numeric HubSpot company ID (cached)."""
        for a in self.get_accounts():
            if a.get("name") == account_name:
                return a.get("hubspot_id")
        return None

    def _lookup_company_name(self, company_id: str | int) -> str | None:
        """Resolve a numeric HubSpot company ID to its display name (cached).
        Returns None if the ID is not one of this CSM's accounts or has no name."""
        cid = str(company_id)
        for a in self.get_accounts():
            if str(a.get("hubspot_id", "")) == cid or a.get("id") == cid:
                return a.get("name") or None
        return None

    def _attach_account_names(self, rows: list[dict], source: str, id_label: str) -> list[dict]:
        """Replace each row's internal company_id/record_id with account_name.

        Rows whose company ID does not resolve to one of this CSM's accounts
        are dropped, so every returned row joins to accounts by name. Only the
        IDs are logged for dropped rows, never record content.
        """
        results = []
        for r in rows:
            company_id = r.pop("company_id", "")
            record_id = r.pop("record_id", "")
            name = self._lookup_company_name(company_id) if company_id else None
            if not name:
                logger.info(
                    "[hubspot_crm] %s: skipping %s=%s, company_id=%s not resolvable to an account name",
                    source, id_label, record_id or "?", company_id or "none",
                )
                continue
            r["account_name"] = name
            results.append(r)
        return results

    def get_contacts(self, account_name: str) -> list[dict]:
        if self._mock:
            account_id = next((a["id"] for a in _MOCK_ACCOUNTS if a["name"] == account_name), None)
            contacts = _MOCK_CONTACTS.get(account_id, [])
            return [
                {"name": c["name"], "email": c["email"], "role": c["title"], "account_name": account_name}
                for c in contacts
            ]
        # Resolve name → numeric HubSpot company ID, then use associatedWith query
        hubspot_id = self._resolve_company_id(account_name)
        if hubspot_id is None:
            logger.warning("[hubspot_crm] get_contacts(%s): could not resolve to numeric ID", account_name)
            return []
        results = self.get_contacts_by_company_id(hubspot_id)
        # Add account_name to each contact (get_contacts_by_company_id doesn't include it)
        for c in results:
            c["account_name"] = account_name
        return results

    def get_contacts_by_company_id(self, hubspot_id: int) -> list[dict]:
        """Fetch contacts by numeric HubSpot company ID using associatedWith filter."""
        if self._mock:
            mock_key = next((a["id"] for a in _MOCK_ACCOUNTS if a.get("hubspot_id") == hubspot_id), None)
            contacts = _MOCK_CONTACTS.get(mock_key or str(hubspot_id), [])
            return [{"name": c["name"], "email": c["email"], "role": c["title"]} for c in contacts]
        try:
            items, _status = _run(self._get_contacts_by_id_async(hubspot_id))
            return items
        except Exception as exc:
            logger.warning("[hubspot_crm] get_contacts_by_company_id(%s) failed: %s", hubspot_id, exc)
            return []

    def get_deals_for_company(self, hubspot_id: int) -> list[dict]:
        """Fetch deals associated with a company by its numeric HubSpot ID."""
        if self._mock:
            mock_key = next((a["id"] for a in _MOCK_ACCOUNTS if a.get("hubspot_id") == hubspot_id), None)
            return _MOCK_DEALS.get(mock_key or str(hubspot_id), [])
        try:
            items, _status = _run(self._get_deals_for_company_async(hubspot_id))
            return items
        except Exception as exc:
            logger.warning("[hubspot_crm] get_deals_for_company(%s) failed: %s", hubspot_id, exc)
            return []

    def get_tickets_for_company(self, hubspot_id: int) -> list[dict]:
        """Fetch open tickets associated with a company by its numeric HubSpot ID."""
        if self._mock:
            mock_key = next((a["id"] for a in _MOCK_ACCOUNTS if a.get("hubspot_id") == hubspot_id), None)
            return _MOCK_TICKETS.get(mock_key or str(hubspot_id), [])
        try:
            items, _status = _run(self._get_tickets_for_company_async(hubspot_id))
            return items
        except Exception as exc:
            logger.warning("[hubspot_crm] get_tickets_for_company(%s) failed: %s", hubspot_id, exc)
            return []

    def get_renewals(self) -> list[dict]:
        if self._mock:
            results = []
            today = date.today()
            for account in _MOCK_ACCOUNTS:
                r = _MOCK_RENEWALS.get(account["name"], {})
                renewal_date_str = r.get("renewal_date", "")
                try:
                    days_until = (date.fromisoformat(renewal_date_str) - today).days
                except (ValueError, TypeError):
                    days_until = None
                results.append({
                    "account_name":       account["name"],
                    "renewal_date":       renewal_date_str,
                    "contract_value":     r.get("contract_value"),
                    "days_until_renewal": days_until,
                })
            return results
        try:
            raw = _run(self._get_renewals_async())
            return self._attach_account_names(raw, "get_renewals", "deal_id")
        except Exception as exc:
            logger.warning("[hubspot_crm] get_renewals failed: %s", exc)
            return []

    def get_health_scores(self) -> list[dict]:
        if self._mock:
            return [
                {
                    "account_name": name,
                    "score":        h["score"],
                    "trend":        h["trend"],
                    "last_updated": h["last_updated"],
                }
                for name, h in _MOCK_HEALTH.items()
            ]
        try:
            return _run(self._get_health_scores_async())
        except Exception as exc:
            logger.warning("[hubspot_crm] get_health_scores failed: %s", exc)
            return []

    def get_open_ctas(self) -> list[dict]:
        if self._mock:
            results = []
            today = date.today()
            for account in _MOCK_ACCOUNTS:
                for cta in _MOCK_CTAS.get(account["id"], []):
                    due_str = cta["due_date"]
                    status = "upcoming"
                    try:
                        due = date.fromisoformat(due_str)
                        if due < today:
                            status = "overdue"
                        elif due == today:
                            status = "due_today"
                    except ValueError:
                        pass
                    results.append({
                        "account_name": account["name"],
                        "title":        cta["title"],
                        "due_date":     due_str,
                        "priority":     cta["priority"],
                        "status":       status,
                        "owner":        self.csm_id,
                    })
            return results
        try:
            raw = _run(self._get_open_ctas_async())
            return self._attach_account_names(raw, "get_open_ctas", "ticket_id")
        except Exception as exc:
            logger.warning("[hubspot_crm] get_open_ctas failed: %s", exc)
            return []

    def get_recent_activity(self, account_name: str, days: int = 14) -> list[dict]:
        if self._mock:
            account_id = next((a["id"] for a in _MOCK_ACCOUNTS if a["name"] == account_name), None)
            activities = _MOCK_ACTIVITIES.get(account_id, [])
            cutoff = date.today() - timedelta(days=days)
            return [
                {"account_name": account_name, "type": a["type"], "summary": a["summary"], "date": a["timestamp"], "author": "sarah_c"}
                for a in activities
                if not a["timestamp"] or date.fromisoformat(a["timestamp"]) >= cutoff
            ]
        try:
            # _get_recent_activity_async now keys on the numeric company ID
            # (associatedWith filter). Resolve the name first; the ABC
            # signature stays name-based for callers like postcall/fallback.
            hubspot_id = self._resolve_company_id(account_name)
            if hubspot_id is None:
                return []
            return _run(self._get_recent_activity_async(hubspot_id, days))
        except Exception as exc:
            logger.warning("[hubspot_crm] get_recent_activity(%s) failed: %s", account_name, exc)
            return []


# ── Factory ───────────────────────────────────────────────────────────────────

def get_hubspot_connector(csm_id: str) -> HubSpotCRMConnector:
    return HubSpotCRMConnector(csm_id)
