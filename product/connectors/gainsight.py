"""
product/connectors/gainsight.py

Gainsight CRM connector using the Gainsight Remote MCP Server.
Authenticates via the OAuth 2.1 token managed in gainsight_mcp.py.

Mirrors product/connectors/hubspot_crm.py architecture — batch session
pooling, circuit breaker, sync/async bridge, mock mode. Tool names are
discovered at runtime via _list_tools(), so the async methods use
_find_tool() with keyword hints and gracefully handle missing tools.

Mock mode: set GAINSIGHT_USE_MOCK=true to return realistic demo data
without connecting to the MCP server.
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
from product.connectors.gainsight_mcp import (
    get_mcp_url,
    load_gainsight_token,
    refresh_gainsight_token,
    save_gainsight_token,
)

logger = logging.getLogger("cs-agent.gainsight")


# ── Exceptions ────────────────────────────────────────────────────────────────

class GainsightToolNotFoundError(RuntimeError):
    """Raised when a required MCP tool can't be resolved via _find_tool
    at runtime. Distinguishes "this tenant doesn't expose the tool we need"
    from generic MCP transport errors — the postcall dispatcher treats
    this as invalid_payload rather than retryable.
    """


# ── Circuit breaker ───────────────────────────────────────────────────────────
_circuit_breaker_until: float = 0.0
_consecutive_429s: int = 0
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN = 300  # seconds


def _check_circuit_breaker() -> bool:
    return time.time() < _circuit_breaker_until


def _record_429():
    global _consecutive_429s, _circuit_breaker_until
    _consecutive_429s += 1
    if _consecutive_429s >= _CIRCUIT_BREAKER_THRESHOLD:
        _circuit_breaker_until = time.time() + _CIRCUIT_BREAKER_COOLDOWN
        logger.warning(
            "Circuit breaker OPEN — pausing all MCP calls for %ds after %d consecutive 429s",
            _CIRCUIT_BREAKER_COOLDOWN, _consecutive_429s,
        )
        _consecutive_429s = 0


def _record_success():
    global _consecutive_429s
    _consecutive_429s = 0


# ── Mock data ─────────────────────────────────────────────────────────────────

_TODAY = date.today()

_MOCK_ACCOUNTS = [
    {"id": "gs-001", "name": "Meridian Health",       "domain": "meridianhealth.example", "industry": "Healthcare",         "owner_id": "sarah_c"},
    {"id": "gs-002", "name": "Beacon Financial",      "domain": "beaconfinancial.example", "industry": "Financial Services", "owner_id": "sarah_c"},
    {"id": "gs-003", "name": "Sutton Retail",         "domain": "suttonretail.example", "industry": "Retail",             "owner_id": "sarah_c"},
    {"id": "gs-004", "name": "Northgate Logistics",   "domain": "northgate.example",    "industry": "Logistics",          "owner_id": "sarah_c"},
    {"id": "gs-005", "name": "Pinnacle Group",        "domain": "pinnaclegroup.example", "industry": "Technology",         "owner_id": "sarah_c"},
]

_MOCK_HEALTH = {
    "Meridian Health":     {"score": 88, "trend": "stable",    "color_code": "green",  "last_updated": str(_TODAY)},
    "Beacon Financial":    {"score": 74, "trend": "declining", "color_code": "yellow", "last_updated": str(_TODAY)},
    "Sutton Retail":       {"score": 85, "trend": "stable",    "color_code": "green",  "last_updated": str(_TODAY)},
    "Northgate Logistics": {"score": 52, "trend": "declining", "color_code": "red",    "last_updated": str(_TODAY)},
    "Pinnacle Group":      {"score": 91, "trend": "improving", "color_code": "green",  "last_updated": str(_TODAY)},
}

_MOCK_RENEWALS = {
    "Meridian Health":     {"renewal_date": str(_TODAY + timedelta(days=120)), "contract_value": 85000},
    "Beacon Financial":    {"renewal_date": str(_TODAY + timedelta(days=90)),  "contract_value": 120000},
    "Sutton Retail":       {"renewal_date": str(_TODAY + timedelta(days=200)), "contract_value": 45000},
    "Northgate Logistics": {"renewal_date": str(_TODAY + timedelta(days=47)),  "contract_value": 95000},
    "Pinnacle Group":      {"renewal_date": str(_TODAY + timedelta(days=180)), "contract_value": 60000},
}

_MOCK_CONTACTS = {
    "gs-001": [{"name": "Marcus Reyes",  "email": "marcus.reyes@meridianhealth.example",  "title": "VP Operations"}],
    "gs-002": [{"name": "Priya Sharma",  "email": "priya.sharma@beaconfinancial.example", "title": "Head of IT"}],
    "gs-003": [{"name": "Jordan Kim",    "email": "jordan.kim@suttonretail.example",      "title": "Director CS"}],
    "gs-004": [{"name": "Dana Mills",    "email": "dana.mills@northgate.example",         "title": "COO"}],
    "gs-005": [{"name": "Alex Wong",     "email": "alex.wong@pinnaclegroup.example",      "title": "CTO"}],
}

_MOCK_CTAS = {
    "Northgate Logistics": [
        {"id": "cta-001", "title": "Executive escalation",    "status": "open",      "due_date": str(_TODAY - timedelta(days=2)), "priority": "high",   "cta_type": "Risk"},
        {"id": "cta-002", "title": "Send updated contract",   "status": "open",      "due_date": str(_TODAY + timedelta(days=2)), "priority": "medium", "cta_type": "Risk"},
    ],
    "Beacon Financial": [
        {"id": "cta-003", "title": "Renewal discovery call",  "status": "open",      "due_date": str(_TODAY + timedelta(days=7)), "priority": "high",   "cta_type": "Opportunity"},
    ],
}

_MOCK_SUCCESS_PLANS = {
    "Meridian Health": [
        {"name": "2026 Platform Adoption", "status": "on_track",  "objectives": ["Train 50 new users", "Rollout Phase 2 modules"]},
    ],
    "Northgate Logistics": [
        {"name": "Renewal Recovery Plan",  "status": "at_risk",   "objectives": ["Re-engage executive sponsor", "Resolve open risk CTAs"]},
    ],
}

_MOCK_TIMELINE = {
    "Meridian Health": [
        {"type": "meeting", "date": str(_TODAY - timedelta(days=3)), "summary": "Monthly check-in — discussed new feature adoption"},
        {"type": "email",   "date": str(_TODAY - timedelta(days=7)), "summary": "Marcus confirmed Q2 goals alignment"},
    ],
    "Beacon Financial": [
        {"type": "email",   "date": str(_TODAY - timedelta(days=8)), "summary": "Priya flagged performance concerns with reporting module"},
    ],
    "Sutton Retail": [
        {"type": "meeting", "date": str(_TODAY - timedelta(days=2)), "summary": "Onboarding review — 3 new users added"},
    ],
    "Northgate Logistics": [],
    "Pinnacle Group": [
        {"type": "note",    "date": str(_TODAY - timedelta(days=1)), "summary": "Alex confirmed executive sponsor buy-in for expansion"},
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

class GainsightCRMConnector(CRMConnector):
    """CRM connector that reads Gainsight data via the Remote MCP Server."""

    def __init__(self, csm_id: str, config: dict | None = None):
        super().__init__(csm_id, config or {})
        self._mock = os.environ.get("GAINSIGHT_USE_MOCK", "").lower() in ("1", "true", "yes")
        self._access_token: str | None = None
        self._tool_names: dict[str, str] | None = None
        self._accounts_cache: list[dict] | None = None
        self._batch_session: ClientSession | None = None
        self._company_fields: dict | None = None  # populated by batch() via get_object_metadata

        if not self._mock:
            self._load_and_refresh_token()

    def _load_and_refresh_token(self) -> None:
        token = load_gainsight_token(self.csm_id)
        if token is None:
            logger.warning("No Gainsight token found for %s — MCP calls will fail", self.csm_id)
            return
        # Gainsight refresh tokens expire in ~1 hour, so refresh with a 5-min buffer.
        if time.time() >= token.get("expires_at", 0) - 300:
            from product.token_lock import token_lock
            with token_lock(self.csm_id, "gainsight"):
                # Double-check with the SAME 300s threshold
                token = load_gainsight_token(self.csm_id)
                if token and time.time() >= token.get("expires_at", 0) - 300:
                    logger.debug("Token near expiry for %s, refreshing", self.csm_id)
                    try:
                        token = refresh_gainsight_token(token["refresh_token"])
                        save_gainsight_token(self.csm_id, token)
                    except Exception as exc:
                        logger.warning("Token refresh failed for %s: %s", self.csm_id, exc)
                        return
        self._access_token = token["access_token"]

    @asynccontextmanager
    async def batch(self):
        """Open a reusable MCP session for multiple tool calls.

        All _mcp_call_tool calls within this block reuse the same TCP connection.
        Outside of this block, each call opens its own session (backward compat).
        """
        # Pre-refresh token with 5-min buffer (Gainsight refresh tokens are short-lived).
        token = load_gainsight_token(self.csm_id)
        if token and time.time() >= token.get("expires_at", 0) - 300:
            from product.token_lock import token_lock_async
            async with token_lock_async(self.csm_id, "gainsight"):
                # Double-check with the SAME 300s threshold
                token = load_gainsight_token(self.csm_id)
                if token and time.time() >= token.get("expires_at", 0) - 300:
                    try:
                        token = refresh_gainsight_token(token["refresh_token"])
                        save_gainsight_token(self.csm_id, token)
                        self._access_token = token["access_token"]
                    except Exception as exc:
                        logger.warning("Pre-batch token refresh failed: %s", exc)
                else:
                    # Another caller refreshed — adopt their token
                    if token:
                        self._access_token = token["access_token"]

        if not self._access_token:
            logger.warning("No access token for batch — yielding without session")
            yield self
            return

        if _check_circuit_breaker():
            logger.warning("Circuit breaker OPEN — yielding without session")
            yield self
            return

        try:
            mcp_url = get_mcp_url()
        except EnvironmentError as exc:
            logger.warning("Cannot open batch session: %s", exc)
            yield self
            return

        async with httpx.AsyncClient(
            headers=self._mcp_headers(),
            timeout=30.0,
        ) as http_client:
            async with streamable_http_client(mcp_url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._batch_session = session
                    try:
                        result = await session.list_tools()
                        self._tool_names = {t.name: (t.description or "") for t in result.tools}
                        logger.info("Batch session opened — %d tools discovered: %s",
                                    len(self._tool_names), list(self._tool_names.keys()))
                    except Exception as exc:
                        logger.warning("Tool discovery in batch failed: %s", exc)
                        self._tool_names = {}
                    # Pre-warm Company field schema so subsequent run_query calls
                    # can use the exact field names. Non-fatal on failure.
                    try:
                        self._company_fields = await self._discover_fields("Company")
                        if isinstance(self._company_fields, dict) and self._company_fields:
                            logger.info("Company metadata discovered — top keys: %s",
                                        list(self._company_fields.keys())[:20])
                        else:
                            logger.info("Company metadata returned non-dict or empty: %s",
                                        type(self._company_fields).__name__)
                    except Exception as exc:
                        logger.warning("Company metadata discovery failed: %s", exc)
                        self._company_fields = {}
                    try:
                        yield self
                    finally:
                        self._batch_session = None
                        logger.info("Batch session closed")

    # ── MCP client ────────────────────────────────────────────────────────────

    def _mcp_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _mcp_call_tool(self, tool_name: str, arguments: dict) -> list:
        """Call a specific MCP tool. Returns the content list from CallToolResult.

        Handles 401 (token refresh + retry) and 429 (back-off + retry up to 2×).
        Adds a 0.2s inter-call delay after each successful response.
        Returns [] on failure.
        """
        if not self._access_token:
            logger.warning("No access token — skipping MCP call to %s", tool_name)
            return []

        if _check_circuit_breaker():
            logger.warning("Circuit breaker OPEN — skipping MCP call to %s", tool_name)
            return []

        # Batch mode: reuse existing session (no new TCP connection)
        if self._batch_session:
            try:
                result = await self._batch_session.call_tool(tool_name, arguments=arguments)
                if result.isError:
                    logger.warning("tool %s returned isError=True (batch): %s", tool_name, result.content)
                    return []
                await asyncio.sleep(0.2)
                _record_success()
                return result.content
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    _record_429()
                    logger.warning("429 in batch on %s, waiting 2s", tool_name)
                    await asyncio.sleep(2.0)
                    try:
                        result = await self._batch_session.call_tool(tool_name, arguments=arguments)
                        if not result.isError:
                            await asyncio.sleep(0.2)
                            _record_success()
                            return result.content
                    except Exception:
                        pass
                logger.warning("Batch call to %s failed: %s", tool_name, exc)
                return []
            except Exception as exc:
                logger.warning("Batch call to %s failed: %s", tool_name, exc)
                return []

        try:
            mcp_url = get_mcp_url()
        except EnvironmentError as exc:
            logger.warning("Cannot open MCP session: %s", exc)
            return []

        for attempt in range(3):
            try:
                logger.debug("call_tool %s args=%s (attempt %d)", tool_name, arguments, attempt + 1)
                async with httpx.AsyncClient(
                    headers=self._mcp_headers(),
                    timeout=30.0,
                ) as http_client:
                    async with streamable_http_client(mcp_url, http_client=http_client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(tool_name, arguments=arguments)
                            if result.isError:
                                logger.warning("tool %s returned isError=True: %s", tool_name, result.content)
                                return []
                            await asyncio.sleep(0.2)
                            _record_success()
                            return result.content
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 401 and attempt == 0:
                    logger.debug("401 on attempt 1, refreshing token and retrying")
                    # Capture the token that just 401'd — used for double-check
                    stale_token = self._access_token
                    from product.token_lock import token_lock_async
                    async with token_lock_async(self.csm_id, "gainsight"):
                        fresh = load_gainsight_token(self.csm_id)
                        if fresh and fresh.get("access_token") != stale_token:
                            # Someone refreshed after our 401 — use theirs
                            self._access_token = fresh["access_token"]
                        elif fresh and fresh.get("refresh_token"):
                            try:
                                token = refresh_gainsight_token(fresh["refresh_token"])
                                save_gainsight_token(self.csm_id, token)
                                self._access_token = token["access_token"]
                            except Exception as refresh_exc:
                                logger.warning("Token refresh on 401 failed: %s", refresh_exc)
                                return []
                    continue
                if status == 429 and attempt < 2:
                    _record_429()
                    wait = 1.0 * (attempt + 1)
                    logger.warning("429 rate limit on %s (attempt %d), retrying in %.1fs", tool_name, attempt + 1, wait)
                    await asyncio.sleep(wait)
                    continue
                logger.warning("HTTP error calling %s: %s", tool_name, exc)
                return []
            except Exception as exc:
                logger.warning("Error calling MCP tool %s: %s", tool_name, exc)
                return []
        return []

    async def _mcp_call_tool_strict(self, tool_name: str, arguments: dict) -> list:
        """Write-path variant of _mcp_call_tool that propagates errors.

        _mcp_call_tool swallows failures into an empty list — correct for
        read paths but wrong for writes where a silent failure would be a
        data-loss bug. This helper raises on missing token, open circuit
        breaker, tool isError, HTTP errors, and any other transport failure.
        No 401 auto-refresh; callers classify the resulting error.
        """
        if not self._access_token:
            raise RuntimeError(f"No Gainsight access token for {self.csm_id}")
        if _check_circuit_breaker():
            raise RuntimeError(
                f"Gainsight circuit breaker OPEN — refusing MCP write call to {tool_name}"
            )

        if self._batch_session:
            result = await self._batch_session.call_tool(tool_name, arguments=arguments)
            if result.isError:
                raise RuntimeError(
                    f"Gainsight tool {tool_name} returned isError=True: {result.content}"
                )
            await asyncio.sleep(0.2)
            _record_success()
            return result.content

        mcp_url = get_mcp_url()
        async with httpx.AsyncClient(headers=self._mcp_headers(), timeout=30.0) as http_client:
            async with streamable_http_client(mcp_url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)
                    if result.isError:
                        raise RuntimeError(
                            f"Gainsight tool {tool_name} returned isError=True: {result.content}"
                        )
                    await asyncio.sleep(0.2)
                    _record_success()
                    return result.content

    async def _list_tools(self) -> dict[str, str]:
        """Discover available tools from the MCP server. Cached after first call."""
        if self._batch_session and self._tool_names is not None:
            return self._tool_names
        if self._tool_names is not None:
            return self._tool_names

        try:
            mcp_url = get_mcp_url()
        except EnvironmentError as exc:
            logger.warning("Cannot list tools: %s", exc)
            self._tool_names = {}
            return {}

        try:
            async with httpx.AsyncClient(
                headers=self._mcp_headers(),
                timeout=30.0,
            ) as http_client:
                async with streamable_http_client(mcp_url, http_client=http_client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        self._tool_names = {t.name: (t.description or "") for t in result.tools}
                        logger.debug("Discovered %d tools: %s", len(self._tool_names), list(self._tool_names.keys()))
                        return self._tool_names
        except Exception as exc:
            logger.warning("_list_tools failed for %s: %s", self.csm_id, exc)
            self._tool_names = {}
            return {}

    def _find_tool(self, *keywords: str) -> str | None:
        """Return the first discovered tool name whose name contains any keyword."""
        if not self._tool_names:
            return None
        for name in self._tool_names:
            for kw in keywords:
                if kw.lower() in name.lower():
                    return name
        return None

    @staticmethod
    def _text_from_content(content: list) -> str:
        parts = []
        for block in content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                parts.append(str(block.data))
        return "\n".join(parts)

    @staticmethod
    def _parse_json_response(content: list) -> list | dict | None:
        text = GainsightCRMConnector._text_from_content(content)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def _discover_fields(self, object_name: str) -> dict:
        """Discover field names for a Gainsight MDA object via get_object_metadata."""
        content = await self._mcp_call_tool(
            "get_object_metadata",
            {"object_name": object_name, "context": f"List all fields on the {object_name} object"},
        )
        parsed = self._parse_json_response(content) or {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _items_from(data) -> list:
        """Extract a result list from common Gainsight response shapes."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Gainsight's run_query / fetch_* tools return {"data": [...]} directly.
            inner = data.get("data")
            if isinstance(inner, list):
                return inner
            # Other common containers as fallback
            for key in ("results", "items", "records", "rows"):
                val = data.get(key)
                if isinstance(val, list):
                    return val
        return []

    # ── Async implementations ────────────────────────────────────────────────
    # Field names confirmed against the live Gainsight sandbox (April 2026).
    # run_query accepts snake_case keys
    # (object_name, select, where, limit) wrapped in a {"query": {...}} envelope
    # and returns {"data": [...]}. Lookup fields are joined with __gr.Name
    # (e.g. Csm__gr.Name). Custom fields carry the __gc suffix
    # (e.g. Days_till_Renewal__gc).

    _EMPTY_WHERE = {"conditions": [], "expression": ""}

    @staticmethod
    def _trend_label(raw, nps=None) -> str:
        """Map Gainsight Trend + Nps into a human-readable trend label.

        Trend "1" → improving, "0" → stable (downgraded to declining if Nps
        is below 50), anything else → declining. Empty raw with no nps
        returns empty string.
        """
        if raw in (None, "") and nps is None:
            return ""
        s = str(raw).strip() if raw not in (None, "") else ""
        if s == "1":
            return "improving"
        # Treat "0" or unknown as stable unless Nps is poor
        try:
            nps_val = float(nps) if nps is not None else None
        except (ValueError, TypeError):
            nps_val = None
        if s == "0":
            if nps_val is not None and nps_val < 50:
                return "declining"
            return "stable"
        return "declining"

    async def _get_accounts_async(self) -> list[dict]:
        """Query the Company object for accounts the CSM manages.

        PICKLIST fields (ManagedAs/Status/Stage) return GSIDs, not labels —
        always select <Field>_PicklistLabel for the human-readable value.
        Filters ManagedAs_PicklistLabel == "Customer" to keep Ex-Customers /
        Prospects / Partners out, but falls back to all rows if no row is
        flagged "Customer" (some sandboxes don't populate ManagedAs).
        """
        # Use only base field names. The MCP server auto-appends the
        # *_PicklistLabel sibling for each PICKLIST field, and __gr.Name LOOKUP
        # joins cause silent query failure — read CSM owner from a separate
        # source (or skip it) until that's resolved.
        await self._list_tools()
        content = await self._mcp_call_tool("run_query", {"query": {
            "object_name": "Company",
            "select": ["Name", "Gsid", "ManagedAs", "Status", "Industry", "Arr", "Mrr"],
            "limit": 100,
        }})
        data = self._parse_json_response(content)
        logger.debug("run_query(Company accounts) raw: %s", str(data)[:500])

        items = [p for p in self._items_from(data) if isinstance(p, dict)]

        def _row_to_account(props: dict) -> dict:
            return {
                "id":          str(props.get("Gsid") or ""),
                "name":        props.get("Name", ""),
                "domain":      "",
                "industry":    props.get("Industry", ""),
                "owner_id":    "",  # Csm__gr.Name LOOKUP join unreliable; resolve elsewhere
                "managed_as":  props.get("ManagedAs_PicklistLabel", ""),
                "status":      props.get("Status_PicklistLabel", ""),
                "stage":       props.get("Stage_PicklistLabel", ""),
                "arr":         props.get("Arr"),
                "mrr":         props.get("Mrr"),
            }

        customers = [
            _row_to_account(p) for p in items
            if p.get("ManagedAs_PicklistLabel") == "Customer"
        ]
        if customers:
            logger.info("_get_accounts_async: %d Customer accounts (from %d rows)", len(customers), len(items))
            return customers

        # Fallback: no rows flagged "Customer" — return everything so the
        # sandbox/user still sees data
        logger.info(
            "_get_accounts_async: no rows matched ManagedAs=Customer, returning all %d rows",
            len(items),
        )
        return [_row_to_account(p) for p in items]

    async def _get_contacts_async(self, account_name: str) -> list[dict]:
        """Query CompanyPerson (Gainsight's junction object) for contacts.

        Falls back to the Person object if CompanyPerson returns empty or
        errors on this tenant. _mcp_call_tool already swallows isError into
        an empty list, so the empty result is the signal to try Person.
        """
        await self._list_tools()

        async def _query(object_name: str) -> list[dict]:
            content = await self._mcp_call_tool("run_query", {"query": {
                "object_name": object_name,
                "select": ["Name", "Email", "Title__gc", "Company__gr.Name"],
                "limit": 50,
            }})
            parsed = self._parse_json_response(content)
            logger.debug("run_query(%s) raw: %s", object_name, str(parsed)[:500])
            return [p for p in self._items_from(parsed) if isinstance(p, dict)]

        items = await _query("CompanyPerson")
        used_object = "CompanyPerson"
        if not items:
            logger.info(
                "CompanyPerson returned no rows for %s — retrying with Person",
                account_name,
            )
            items = await _query("Person")
            used_object = "Person"

        results = []
        for props in items:
            if props.get("Company__gr.Name") != account_name:
                continue
            results.append({
                "name":         props.get("Name", ""),
                "email":        props.get("Email", ""),
                "role":         props.get("Title__gc", ""),
                "account_name": account_name,
            })
        logger.info("_get_contacts_async(%s): %d contacts via %s", account_name, len(results), used_object)
        return results

    async def _get_renewals_async(self) -> list[dict]:
        """Query the Company object for renewal fields.

        Days_till_Renewal__gc is a pre-computed custom field, so we use it
        directly when present and fall back to RenewalDate math otherwise.
        """
        await self._list_tools()
        content = await self._mcp_call_tool("run_query", {"query": {
            "object_name": "Company",
            "select": ["Name", "RenewalDate", "Days_till_Renewal__gc", "Arr", "Mrr"],
            "limit": 100,
        }})
        data = self._parse_json_response(content)
        logger.debug("run_query(Company renewals) raw: %s", str(data)[:500])

        results = []
        today = date.today()
        for props in self._items_from(data):
            if not isinstance(props, dict):
                continue
            renewal_raw = props.get("RenewalDate") or ""
            days_until = props.get("Days_till_Renewal__gc")
            renewal_str = ""
            if renewal_raw:
                try:
                    renewal_date = date.fromisoformat(str(renewal_raw)[:10])
                    renewal_str = str(renewal_date)
                    if days_until is None:
                        days_until = (renewal_date - today).days
                except (ValueError, TypeError):
                    pass
            if not renewal_str and days_until is None:
                continue
            results.append({
                "account_name":       props.get("Name", ""),
                "renewal_date":       renewal_str,
                "contract_value":     props.get("Arr"),
                "days_until_renewal": days_until,
            })
        logger.info("_get_renewals_async: %d renewals", len(results))
        return results

    async def _get_health_scores_async(self) -> list[dict]:
        """Query the Company object for health proxy fields.

        Sandbox confirmed Nps is the reliable numeric health indicator
        (e.g. 49.34, 100.0, 70.41). The CurrentScore LOOKUP join was
        unreliable across tenants so we drop it. Trend label combines
        Trend + Nps so 0/low-Nps maps to "declining".
        """
        await self._list_tools()
        content = await self._mcp_call_tool("run_query", {"query": {
            "object_name": "Company",
            "select": ["Name", "Nps", "Csat", "Trend"],
            "limit": 100,
        }})
        data = self._parse_json_response(content)
        logger.debug("run_query(Company health) raw: %s", str(data)[:500])

        results = []
        for props in self._items_from(data):
            if not isinstance(props, dict):
                continue
            nps = props.get("Nps")
            results.append({
                "account_name": props.get("Name", ""),
                "score":        nps,
                "previous":     "",
                "color_code":   "",
                "trend":        self._trend_label(props.get("Trend"), nps=nps),
                "nps":          nps,
                "csat":         props.get("Csat"),
                "last_updated": "",
            })
        logger.info("_get_health_scores_async: %d health records", len(results))
        return results

    async def _get_open_ctas_async(self) -> list[dict]:
        """Fetch all CTAs and filter IsClosed in Python.

        fetch_cta_list with an empty WhereClause returns every CTA; server-
        side status filters kept rejecting in discovery so we filter locally.
        """
        await self._list_tools()
        content = await self._mcp_call_tool("fetch_cta_list", {"where": self._EMPTY_WHERE})
        data = self._parse_json_response(content)
        logger.debug("fetch_cta_list raw: %s", str(data)[:500])

        items = self._items_from(data)
        results = []
        today = date.today()
        for props in items:
            if not isinstance(props, dict):
                continue
            if props.get("IsClosed") is True:
                continue
            due_raw = props.get("DueDate") or ""
            due_str = str(due_raw)[:10] if due_raw else ""
            due_status = "upcoming"
            if due_str:
                try:
                    due_date = date.fromisoformat(due_str)
                    if due_date < today:
                        due_status = "overdue"
                    elif due_date == today:
                        due_status = "due_today"
                except ValueError:
                    pass
            results.append({
                "account_name": props.get("CompanyId__gr.Name", ""),
                "title":        props.get("Name", ""),
                "due_date":     due_str,
                "priority":     (props.get("PriorityId__gr.Name") or "medium").lower(),
                "status":       props.get("StatusId__gr.Name", ""),
                "due_status":   due_status,
                "cta_type":     props.get("TypeId__gr.Name", ""),
                "reason":       props.get("ReasonId__gr.Name", ""),
                "owner":        props.get("OwnerId__gr.Name", ""),
                "owner_email":  props.get("OwnerId__gr.Email", ""),
            })
        logger.info("_get_open_ctas_async: %d open CTAs (from %d total)", len(results), len(items))
        return results

    async def _get_recent_activity_async(self, account_name: str, days: int) -> list[dict]:
        """Fetch all timeline activities and filter by company name + recency."""
        await self._list_tools()
        content = await self._mcp_call_tool(
            "fetch_timeline_activity_list", {"where": self._EMPTY_WHERE},
        )
        data = self._parse_json_response(content)
        logger.debug("fetch_timeline_activity_list raw: %s", str(data)[:500])

        items = self._items_from(data)
        results = []
        cutoff = date.today() - timedelta(days=days)
        for props in items:
            if not isinstance(props, dict):
                continue
            if props.get("CompanyName") != account_name:
                continue
            ts_raw = props.get("ActivityDate") or ""
            ts_str = str(ts_raw)[:10] if ts_raw else ""
            try:
                if ts_str and date.fromisoformat(ts_str) < cutoff:
                    continue
            except ValueError:
                pass
            notes = (props.get("Notes") or "")[:200]
            results.append({
                "account_name": account_name,
                "type":         (props.get("TypeName") or "activity").lower(),
                "summary":      props.get("Subject", ""),
                "notes":        notes,
                "date":         ts_str,
                "author":       props.get("AuthorName", ""),
            })
        logger.info(
            "_get_recent_activity_async(%s, %dd): %d activities (from %d total)",
            account_name, days, len(results), len(items),
        )
        return results

    async def _get_success_plans_async(self, account_name: str) -> list[dict]:
        """Fetch all success plans and filter by company name.

        Field names inside a success plan record are still unconfirmed; keep
        best-effort parsing and log raw so we can tighten in a follow-up.
        """
        await self._list_tools()
        content = await self._mcp_call_tool(
            "fetch_success_plan_list", {"where": self._EMPTY_WHERE},
        )
        data = self._parse_json_response(content)
        logger.debug("fetch_success_plan_list raw: %s", str(data)[:500])

        items = self._items_from(data)
        results = []
        for props in items:
            if not isinstance(props, dict):
                continue
            props_account = (
                props.get("CompanyName")
                or props.get("CompanyId__gr.Name")
                or props.get("Company__gr.Name")
                or ""
            )
            if props_account and props_account != account_name:
                continue
            results.append({
                "account_name": account_name,
                "name":         props.get("Name", props.get("name", "")),
                "status":       (props.get("Status") or props.get("StatusName") or "").lower(),
                "objectives":   props.get("Objectives", props.get("objectives", [])),
            })
        logger.info(
            "_get_success_plans_async(%s): %d plans (from %d total)",
            account_name, len(results), len(items),
        )
        return results

    # ── ABC interface (sync) ──────────────────────────────────────────────────

    def get_accounts(self) -> list[dict]:
        if self._accounts_cache is not None:
            return self._accounts_cache
        if self._mock:
            self._accounts_cache = [
                {"id": a["id"], "name": a["name"], "tier": "standard", "owner": self.csm_id}
                for a in _MOCK_ACCOUNTS
            ]
            return self._accounts_cache
        try:
            raw = _run(self._get_accounts_async())
            self._accounts_cache = [
                {"id": a["id"], "name": a["name"], "tier": "standard", "owner": a.get("owner_id", "")}
                for a in raw
            ]
            return self._accounts_cache
        except Exception as exc:
            logger.warning("get_accounts failed: %s", exc)
            return []

    def get_contacts(self, account_name: str) -> list[dict]:
        if self._mock:
            account_id = next((a["id"] for a in _MOCK_ACCOUNTS if a["name"] == account_name), None)
            contacts = _MOCK_CONTACTS.get(account_id, [])
            return [
                {"name": c["name"], "email": c["email"], "role": c["title"], "account_name": account_name}
                for c in contacts
            ]
        try:
            return _run(self._get_contacts_async(account_name))
        except Exception as exc:
            logger.warning("get_contacts(%s) failed: %s", account_name, exc)
            return []

    def get_renewals(self) -> list[dict]:
        if self._mock:
            results = []
            today = date.today()
            for account in _MOCK_ACCOUNTS:
                r = _MOCK_RENEWALS.get(account["name"], {})
                renewal_str = r.get("renewal_date", "")
                try:
                    days_until = (date.fromisoformat(renewal_str) - today).days
                except (ValueError, TypeError):
                    days_until = None
                results.append({
                    "account_name":       account["name"],
                    "renewal_date":       renewal_str,
                    "contract_value":     r.get("contract_value"),
                    "days_until_renewal": days_until,
                })
            return results
        try:
            return _run(self._get_renewals_async())
        except Exception as exc:
            logger.warning("get_renewals failed: %s", exc)
            return []

    def get_health_scores(self) -> list[dict]:
        if self._mock:
            return [
                {
                    "account_name": name,
                    "score":        h["score"],
                    "trend":        h["trend"],
                    "color_code":   h["color_code"],
                    "last_updated": h["last_updated"],
                }
                for name, h in _MOCK_HEALTH.items()
            ]
        try:
            return _run(self._get_health_scores_async())
        except Exception as exc:
            logger.warning("get_health_scores failed: %s", exc)
            return []

    def get_open_ctas(self) -> list[dict]:
        if self._mock:
            results = []
            today = date.today()
            for account_name, ctas in _MOCK_CTAS.items():
                for cta in ctas:
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
                        "account_name": account_name,
                        "title":        cta["title"],
                        "due_date":     due_str,
                        "priority":     cta["priority"],
                        "status":       status,
                        "cta_type":     cta["cta_type"],
                        "owner":        self.csm_id,
                    })
            return results
        try:
            return _run(self._get_open_ctas_async())
        except Exception as exc:
            logger.warning("get_open_ctas failed: %s", exc)
            return []

    def get_recent_activity(self, account_name: str, days: int = 14) -> list[dict]:
        if self._mock:
            activities = _MOCK_TIMELINE.get(account_name, [])
            cutoff = date.today() - timedelta(days=days)
            return [
                {"account_name": account_name, "type": a["type"], "summary": a["summary"], "date": a["date"], "author": "sarah_c"}
                for a in activities
                if not a["date"] or date.fromisoformat(a["date"]) >= cutoff
            ]
        try:
            return _run(self._get_recent_activity_async(account_name, days))
        except Exception as exc:
            logger.warning("get_recent_activity(%s) failed: %s", account_name, exc)
            return []

    # ── Write path: timeline activity ────────────────────────────────────────

    async def _create_timeline_activity_async(
        self,
        *,
        account_id: str,
        account_name: str,
        notes: str,
        subject: str,
    ) -> dict:
        """Discover the MCP write tool at runtime and call it with strict
        error propagation. Returns a {"status", "activity_id"} dict.
        """
        await self._list_tools()
        tool_name = self._find_tool(
            "create_timeline_activity",
            "add_timeline_activity",
            "timeline_activity",
        )
        if not tool_name:
            known = list((self._tool_names or {}).keys())
            raise GainsightToolNotFoundError(
                f"no MCP tool matching timeline-activity create keywords; "
                f"available tools={known}"
            )

        content = await self._mcp_call_tool_strict(tool_name, {
            "company_id": account_id,
            "subject": subject,
            "notes": notes,
        })
        parsed = self._parse_json_response(content)

        # Activity-id field shape is tenant-dependent; look across the
        # common spellings Gainsight returns and unwrap the "data" envelope
        # that run_query-style responses use.
        activity_id: str | None = None
        candidates = parsed if isinstance(parsed, dict) else {}
        for key in ("id", "activity_id", "ActivityId", "Gsid"):
            value = candidates.get(key)
            if value:
                activity_id = str(value)
                break
        if not activity_id and isinstance(candidates.get("data"), dict):
            inner = candidates["data"]
            for key in ("id", "activity_id", "ActivityId", "Gsid"):
                value = inner.get(key)
                if value:
                    activity_id = str(value)
                    break
        if not activity_id:
            raise RuntimeError(
                f"Gainsight {tool_name} returned no activity id: {parsed!r}"
            )
        return {"status": "created", "activity_id": activity_id}

    def create_timeline_activity(
        self,
        *,
        account_id: str,
        account_name: str,
        notes: str,
        subject: str | None = None,
    ) -> dict:
        """Write a timeline activity to Gainsight for an account.

        Args:
            account_id: Gainsight Gsid of the Company object.
            account_name: Display name (for mock-mode logging + readability).
            notes: Body of the activity (plain text).
            subject: Optional subject / title. Defaults to "Post-call recap".

        Returns:
            {"status": "created", "activity_id": <id>}

        Raises:
            GainsightToolNotFoundError: tenant doesn't expose a matching
                write tool via MCP _list_tools.
            RuntimeError / httpx errors: transport or server-side failures
                propagate; caller classifies.
        """
        subject = subject or "Post-call recap"
        if self._mock:
            import uuid as _uuid
            mock_id = f"mock-gs-activity-{_uuid.uuid4().hex[:8]}"
            logger.info(
                "[gainsight/mock] create_timeline_activity account=%s subject=%s",
                account_name, subject,
            )
            return {"status": "created", "activity_id": mock_id}

        return _run(self._create_timeline_activity_async(
            account_id=account_id,
            account_name=account_name,
            notes=notes,
            subject=subject,
        ))

    def get_success_plans(self, account_name: str) -> list[dict]:
        """Gainsight-specific: return Success Plans for an account. Not part of the CRMConnector ABC."""
        if self._mock:
            return [
                {
                    "account_name": account_name,
                    "name":         p["name"],
                    "status":       p["status"],
                    "objectives":   p["objectives"],
                }
                for p in _MOCK_SUCCESS_PLANS.get(account_name, [])
            ]
        try:
            return _run(self._get_success_plans_async(account_name))
        except Exception as exc:
            logger.warning("get_success_plans(%s) failed: %s", account_name, exc)
            return []


# ── Factory ───────────────────────────────────────────────────────────────────

def get_gainsight_connector(csm_id: str, config: dict | None = None) -> GainsightCRMConnector:
    return GainsightCRMConnector(csm_id, config)
