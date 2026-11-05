"""
product/chat_tools/registry.py

Tool definitions and dispatcher for the tool-use chat handler.

The new chat handler (planned, not yet wired) will use Anthropic's tool-use API
to let Claude call data-fetching tools during a turn instead of having data
pre-stuffed into the system prompt. This module is the single source of truth
for which tools exist, their JSON Schema input shapes, and where dispatch
sends them.

Each tool is intentionally a pure data fetch with no side effects. Action
emission (draft cards, write-backs) stays in the LLM's text output via the
existing <<<DRAFT_CARD>>> protocol; tools never write.

Wired tools follow a never-raise convention: on failure they return a dict
containing an "error" key plus the empty-shape defaults for the rest of
the response. The chat handler converts that into a tool_result with
is_error so the model can recover gracefully.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os

from product.logging_config import request_id_var

logger = logging.getLogger("cs-agent.chat-tools")


def _read_timeout_env(var_name: str, default: float) -> float:
    """Read a positive float from env, log WARN and return default on bad input."""
    raw = os.environ.get(var_name)
    if raw is None or raw == "":
        return default
    try:
        parsed = float(raw)
    except ValueError:
        logger.warning(
            "[chat-tools] env %s=%r is not a float; using default %.1fs",
            var_name, raw, default,
        )
        return default
    if parsed <= 0:
        logger.warning(
            "[chat-tools] env %s=%r is not positive; using default %.1fs",
            var_name, raw, default,
        )
        return default
    return parsed


DEFAULT_TOOL_TIMEOUT_S: float = _read_timeout_env("CHAT_TOOL_TIMEOUT_S", 30.0)
PER_TOOL_TIMEOUTS_S: dict[str, float] = {}
# Override only when justified by production p95 telemetry. Empty by default.
# Add entries with a comment citing the latency data they are sized against.


TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_calendar_events",
        "description": "Fetch customer and internal meetings from the CSM's primary Google Calendar. Returns events with start time, account attribution, attendees, and prep block status, all in the CSM's local timezone. Pass no arguments for today only (single-day shape). Pass start_date for a single specific day, or start_date and end_date for a range up to 30 days. Use this when the user asks about meetings, calendar items, schedule, today's events, tomorrow, next week, or what's on their day.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Optional ISO date YYYY-MM-DD interpreted in the CSM's timezone. Omit for today only. If provided without end_date, queries that single day.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Optional ISO date YYYY-MM-DD. Inclusive upper bound of a range query. Must be on or after start_date. Range span is hard capped at 30 days. Cannot be supplied without start_date.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_my_accounts",
        "description": "Return the authenticated CSM's book of business: the list of account names they manage. Use this to check whether an account the user mentioned is in their portfolio, or to enumerate accounts when the user asks 'what accounts do I have'.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_account_dossier",
        "description": "Fetch a complete dossier for one account from HubSpot: contacts, deals, open tickets, and recent timeline activities. Use this when the user asks about a specific account by name, asks 'what's happening with X', or asks for context on an account ahead of a meeting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_name": {
                    "type": "string",
                    "description": "The account name as it appears in the CSM's portfolio. Must be an exact match against list_my_accounts output.",
                },
            },
            "required": ["account_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_contact_email",
        "description": "Look up a contact's email address from HubSpot by name. Optionally scope to a specific account. Use this BEFORE drafting an email or fetching email threads when the user names a contact instead of providing an email address.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Full name or first name of the contact. Case-insensitive substring match.",
                },
                "account_name": {
                    "type": "string",
                    "description": "Optional: limit the search to one account. Recommended when known, to avoid ambiguity across accounts.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_threads_with_contact",
        "description": "Fetch recent Gmail threads to/from a specific contact email address. Returns thread metadata only (subject, dates, direction, message count). Never returns message bodies. Use this when the user asks about emails with a specific person.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_email": {
                    "type": "string",
                    "description": "The email address of the contact. Must be a valid email.",
                },
                "days": {
                    "type": "integer",
                    "description": "Look-back window in days. Default 14, max 90.",
                    "minimum": 1,
                    "maximum": 90,
                    "default": 14,
                },
            },
            "required": ["contact_email"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_recent_transcripts",
        "description": "Search the CSM's Google Drive for recent meeting transcript Docs. Returns a list of matching transcripts with file_id, name, modified date, and a short snippet. Use this when the user asks about a past meeting, what was discussed, or what was said in a recent call. After finding transcripts, you can call read_transcript_excerpt to get the full content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional: a contact name, account name, or topic to filter transcripts by name. Leave empty to list all recent transcripts.",
                },
                "days": {
                    "type": "integer",
                    "description": "Look-back window in days. Default 14, max 60.",
                    "minimum": 1,
                    "maximum": 60,
                    "default": 14,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_transcript_excerpt",
        "description": "Read the body content of a specific transcript Doc from Google Drive. Returns up to max_chars of the transcript text. Use this AFTER find_recent_transcripts identifies the right Doc, to get the actual conversation content for summarization.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The Google Drive file ID returned by find_recent_transcripts.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return. Default 4000, max 8000. Larger values may be truncated.",
                    "minimum": 500,
                    "maximum": 8000,
                    "default": 4000,
                },
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "slack_draft_message",
        "description": "Draft a Slack message to a teammate, channel, or DM on behalf of the CSM. The draft is shown to the CSM for approval before anything is sent. Use when the CSM wants to notify a colleague, post to a channel, or follow up internally about an account. This tool only drafts - it never sends. The CSM approves the draft card, which sends via a separate endpoint.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {
                    "type": "string",
                    "description": "Slack channel name (e.g. #customer-success) or teammate name.",
                },
                "message": {
                    "type": "string",
                    "description": "The draft message text.",
                },
                "account_context": {
                    "type": "string",
                    "description": "The account this message relates to, if any.",
                },
            },
            "required": ["recipient", "message"],
            "additionalProperties": False,
        },
    },
]


_TOOL_NAMES: set[str] = {t["name"] for t in TOOL_DEFINITIONS}


def get_tools_with_cache_control() -> list[dict]:
    """Return a deep copy of TOOL_DEFINITIONS with cache_control set on the
    last entry.

    Anthropic prompt caching breaks at cache_control markers. Placing one
    on the last tool means the entire tools array is cached after the first
    request in a session, dropping per-turn input cost roughly 10x for the
    tools portion. Original TOOL_DEFINITIONS is not mutated.
    """
    tools = copy.deepcopy(TOOL_DEFINITIONS)
    if tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


async def dispatch_tool(name: str, input: dict, csm_id: str) -> dict:
    """Dispatch a tool call by name. Returns the tool's output dict.

    Raises ValueError if the tool name is not a known tool (programmer
    error in the chat handler; the call site catches Exception and
    surfaces it as is_error: True at the loop level). Raises
    NotImplementedError if the tool is known but not yet wired.

    Each tool implementation is responsible for its own error handling.
    A wired tool returns a dict with an "error" key on failure rather
    than raising, so the LLM can recover gracefully via tool_result.

    On timeout (DEFAULT_TOOL_TIMEOUT_S, overridable per-tool via
    PER_TOOL_TIMEOUTS_S or globally via CHAT_TOOL_TIMEOUT_S env), the
    wrapper returns a dict with "error" and "timed_out": True so the LLM
    sees a structured failure and the iteration loop continues.
    """
    if name not in _TOOL_NAMES:
        raise ValueError(f"unknown tool: {name}")

    timeout_s = PER_TOOL_TIMEOUTS_S.get(name, DEFAULT_TOOL_TIMEOUT_S)

    # Note: cumulative wall-clock across iterations is intentionally
    # unbounded by this wrapper. With MAX_ITERATIONS=4 and every tool
    # timing out, total request wall-clock can reach ~4 x timeout_s.
    # Follow-up: P2 card "Cumulative wall-clock cap on tool dispatch
    # within iteration loop".
    #
    # Note: when wait_for fires while a tool is mid asyncio.to_thread
    # (calendar, gmail, read_transcript_excerpt), the wrapped sync
    # googleapiclient call continues running on its worker thread until
    # the underlying socket gives up. The result is discarded but the
    # thread is leaked until socket-level completion. Real fix is
    # socket-level timeouts on googleapiclient. Follow-up: P2 card
    # "googleapiclient socket-level timeouts".
    try:
        return await asyncio.wait_for(
            _dispatch_inner(name, input, csm_id),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        rid = request_id_var.get("-")
        logger.warning(
            "[chat-tools] tool=%s TIMEOUT after %.1fs csm_id=%s rid=%s",
            name, timeout_s, csm_id, rid,
        )
        return {
            "error": f"tool {name} timed out after {timeout_s:.0f}s (request_id: {rid})",
            "timed_out": True,
        }


async def _dispatch_inner(name: str, input: dict, csm_id: str) -> dict:
    """Tool routing body. Caller is dispatch_tool, which validates the
    name. Factored out so the next commit can wrap this in
    asyncio.wait_for without restructuring the if-chain.
    """
    if name == "get_calendar_events":
        return await _tool_get_calendar_events(input, csm_id)
    if name == "list_my_accounts":
        return await _tool_list_my_accounts(csm_id)
    if name == "resolve_contact_email":
        return await _tool_resolve_contact_email(input, csm_id)
    if name == "find_threads_with_contact":
        return await _tool_find_threads_with_contact(input, csm_id)
    if name == "get_account_dossier":
        return await _tool_get_account_dossier(input, csm_id)
    if name == "find_recent_transcripts":
        return await _tool_find_recent_transcripts(input, csm_id)
    if name == "read_transcript_excerpt":
        return await _tool_read_transcript_excerpt(input, csm_id)
    if name == "slack_draft_message":
        return await _tool_slack_draft_message(input, csm_id)

    raise NotImplementedError(f"not yet wired: {name}")


# Tool implementations
# Each implementation is a private async function so dispatch_tool stays
# a thin router. Helpers shared across tools live above the implementations.


def _empty_calendar_result(error: str | None = None) -> dict:
    """Empty-shape calendar result. Used as the no-data fallback."""
    out: dict = {
        "date": None,
        "customer_meetings": [],
        "internal_meetings": [],
        "total_customer_meetings": 0,
        "total_meetings": 0,
    }
    if error is not None:
        out["error"] = error[:200]
    return out


def _load_account_names_for_csm(csm_id: str) -> list[str]:
    """Load the CSM's account names. DB primary, file fallback.

    Returns [] when neither source has accounts. Never raises; logs at
    DEBUG on partial failures because account-load failure is a normal
    state for users mid-onboarding.

    Mirrors the load order used by the digest pipeline in main.py so a
    fresh user with accounts saved during onboarding gets the same list
    in chat-tool context as in the digest.
    """
    accounts: list[str] = []

    # 1. DB
    try:
        from product.database import Customer, SessionLocal
        if SessionLocal is not None:
            session = SessionLocal()
            try:
                row = session.query(Customer).filter_by(clerk_user_id=csm_id).first()
                if row and row.accounts:
                    accounts = list(row.accounts)
            finally:
                session.close()
    except Exception as exc:
        logger.debug("[chat-tools] DB account load failed for %s: %s", csm_id, exc)

    if accounts:
        return accounts

    # 2. File fallback
    try:
        from product.csm_id_validation import safe_data_file
        path = safe_data_file("product/data/accounts", csm_id)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            file_accounts = data.get("accounts", [])
            if isinstance(file_accounts, list):
                accounts = [str(a) for a in file_accounts if a]
    except Exception as exc:
        logger.debug("[chat-tools] file account load failed for %s: %s", csm_id, exc)

    return accounts


_CALENDAR_TOOL_MAX_RANGE_DAYS = 30


async def _tool_get_calendar_events(input: dict, csm_id: str) -> dict:
    """Implementation of the get_calendar_events tool.

    Three input modes:
      1. start_date and end_date both omitted: fetches today in the CSM's
         local timezone via the legacy extract_calendar_signals shim.
         Returns the slimmed legacy single-day shape.
      2. start_date only (or start_date == end_date): single-day range
         via extract_calendar_range. Returns the range shape with one
         by_date bucket.
      3. Both start_date and end_date: inclusive range up to 30 days,
         via extract_calendar_range. Returns the range shape.

    end_date without start_date is rejected as invalid.

    All ISO dates (YYYY-MM-DD) are interpreted in the CSM's stored
    timezone, not UTC.

    Manual smoke check from a Python REPL:
        import asyncio
        from product.chat_tools.registry import dispatch_tool
        result = asyncio.run(dispatch_tool("get_calendar_events", {}, "sarah_c"))
        print(result)
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"start_date": "2026-05-04", "end_date": "2026-05-08"},
            "sarah_c",
        ))
        print(result)
    """
    from datetime import date as _date

    try:
        from product.signals.calendar import (
            extract_calendar_range,
            extract_calendar_signals,
        )
    except Exception as exc:
        logger.warning("[chat-tools] calendar import failed: %s", exc)
        return _empty_calendar_result(error=f"import_failed: {exc}")

    raw_start = input.get("start_date")
    raw_end = input.get("end_date")
    start_str = raw_start.strip() if isinstance(raw_start, str) and raw_start.strip() else None
    end_str = raw_end.strip() if isinstance(raw_end, str) and raw_end.strip() else None

    if end_str is not None and start_str is None:
        return _empty_calendar_result(
            error="end_date provided without start_date; pass start_date or both",
        )

    try:
        account_names = _load_account_names_for_csm(csm_id)
    except Exception as exc:
        logger.warning("[chat-tools] account load failed for %s: %s", csm_id, exc)
        account_names = []

    # Mode 1: today, legacy shim
    if start_str is None and end_str is None:
        try:
            signal = await asyncio.to_thread(
                extract_calendar_signals, csm_id, account_names,
            )
        except Exception as exc:
            logger.warning("[chat-tools] get_calendar_events today path failed for %s: %s", csm_id, exc)
            return _empty_calendar_result(error=str(exc))

        if not isinstance(signal, dict):
            return _empty_calendar_result(error="extractor_returned_non_dict")

        return {
            "date": signal.get("date"),
            "customer_meetings": signal.get("customer_meetings", []),
            "internal_meetings": signal.get("internal_meetings", []),
            "total_customer_meetings": signal.get("total_customer_meetings", 0),
            "total_meetings": signal.get("total_meetings", 0),
        }

    # Modes 2 and 3: range
    try:
        start_d = _date.fromisoformat(start_str)
    except (TypeError, ValueError):
        return _empty_calendar_result(
            error=f"invalid start_date {start_str!r}, expected YYYY-MM-DD",
        )

    if end_str is None:
        end_d = start_d
    else:
        try:
            end_d = _date.fromisoformat(end_str)
        except (TypeError, ValueError):
            return _empty_calendar_result(
                error=f"invalid end_date {end_str!r}, expected YYYY-MM-DD",
            )

    if start_d > end_d:
        return _empty_calendar_result(
            error=f"start_date {start_d.isoformat()} is after end_date {end_d.isoformat()}",
        )

    span_days = (end_d - start_d).days + 1
    if span_days > _CALENDAR_TOOL_MAX_RANGE_DAYS:
        return _empty_calendar_result(
            error=(
                f"range exceeds {_CALENDAR_TOOL_MAX_RANGE_DAYS}-day cap; "
                f"requested {span_days} days. Narrow the range."
            ),
        )

    try:
        signal = await asyncio.to_thread(
            extract_calendar_range, csm_id, account_names, start_d, end_d,
        )
    except Exception as exc:
        logger.warning("[chat-tools] get_calendar_events range path failed for %s: %s", csm_id, exc)
        return _empty_calendar_result(error=str(exc))

    if not isinstance(signal, dict):
        return _empty_calendar_result(error="extractor_returned_non_dict")

    return signal


async def _tool_list_my_accounts(csm_id: str) -> dict:
    """Implementation of the list_my_accounts tool.

    Reads from Customer.accounts column in Postgres as the primary source,
    falls back to product/data/accounts/{csm_id}.json on the filesystem
    when the DB row is missing or has no accounts. Returns
    {"accounts": [...]} or {"accounts": [], "error": "..."} on hard
    failure.

    Reuses _load_account_names_for_csm so the load order matches the
    digest pipeline and the calendar tool. The tool just wraps that
    helper in a tool-shaped response.

    Manual smoke check from a Python REPL:
        import asyncio
        from product.chat_tools.registry import dispatch_tool
        result = asyncio.run(dispatch_tool("list_my_accounts", {}, "sarah_c"))
        print(result)
    """
    try:
        accounts = await asyncio.to_thread(_load_account_names_for_csm, csm_id)
    except Exception as exc:
        logger.warning("[chat-tools] list_my_accounts failed for %s: %s", csm_id, exc)
        return {"accounts": [], "error": str(exc)[:200]}

    return {"accounts": accounts}


def _name_matches(query: str, candidate: str) -> bool:
    """Case-insensitive substring match. Empty candidate never matches."""
    if not candidate:
        return False
    return query.strip().lower() in candidate.strip().lower()


async def _tool_resolve_contact_email(input: dict, csm_id: str) -> dict:
    """Implementation of the resolve_contact_email tool.

    Two paths:
      1. account_name supplied: single get_contacts call scoped to that
         account, then substring-match on contact name.
      2. account_name omitted: open an async batch session, fetch the
         first 10 accounts, then per-account contacts in series, then
         match across the union.

    Returns {"matches": [...]} on success. On any failure returns
    {"matches": [], "error": "..."}. The "hubspot_not_connected" error
    surfaces when no token is on file (typical for users mid-onboarding).
    """
    raw_name = input.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return {"matches": [], "error": "missing_or_invalid_name"}
    query_name = raw_name.strip()

    raw_account = input.get("account_name")
    account_name = raw_account.strip() if isinstance(raw_account, str) and raw_account.strip() else None

    try:
        from product.connectors.hubspot_mcp import load_hubspot_token
        from product.connectors.hubspot_crm import HubSpotCRMConnector
    except Exception as exc:
        logger.warning("[chat-tools] resolve_contact_email import failed: %s", exc)
        return {"matches": [], "error": f"import_failed: {exc}"}

    try:
        token = await asyncio.to_thread(load_hubspot_token, csm_id)
        if not token:
            return {"matches": [], "error": "hubspot_not_connected"}

        connector = HubSpotCRMConnector(csm_id)

        if account_name is not None:
            contacts = await asyncio.to_thread(connector.get_contacts, account_name)
            matches = [
                {
                    "name": c.get("name", ""),
                    "email": c.get("email", ""),
                    "role": c.get("role", ""),
                    "account_name": c.get("account_name", account_name),
                }
                for c in (contacts or [])
                if _name_matches(query_name, c.get("name", ""))
            ]
            return {"matches": matches}

        async with connector.batch():
            accounts = await connector._get_accounts_async()
            matches: list[dict] = []
            for acct in (accounts or [])[:10]:
                hubspot_id = acct.get("hubspot_id")
                acct_name = acct.get("name", "")
                if hubspot_id is None:
                    continue
                contacts, _status = await connector._get_contacts_by_id_async(hubspot_id)
                for c in contacts or []:
                    if _name_matches(query_name, c.get("name", "")):
                        matches.append({
                            "name": c.get("name", ""),
                            "email": c.get("email", ""),
                            "role": c.get("role", ""),
                            "account_name": acct_name,
                        })
        return {"matches": matches}
    except Exception as exc:
        logger.warning(
            "[chat-tools] resolve_contact_email failed for %s name=%r account=%r: %s",
            csm_id, query_name, account_name, exc,
        )
        return {"matches": [], "error": str(exc)[:200]}


async def _tool_find_threads_with_contact(input: dict, csm_id: str) -> dict:
    """Implementation of the find_threads_with_contact tool.

    Validates the contact_email shape and clamps the days look-back to
    [1, 90], then dispatches to extract_threads_for_contact in
    product/signals/gmail.py via asyncio.to_thread. Email body content
    is never returned, consistent with the format=metadata enforcement
    in the Gmail extractor.

    On extractor exceptions, returns {"threads": [], "error": "..."} so
    the LLM can continue.
    """
    raw_email = input.get("contact_email")
    if not isinstance(raw_email, str) or "@" not in raw_email:
        return {
            "contact_email": raw_email if isinstance(raw_email, str) else "",
            "threads": [],
            "error": "missing_or_invalid_contact_email",
        }
    contact_email = raw_email.strip()

    raw_days = input.get("days", 14)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = 14
    if days < 1:
        days = 1
    if days > 90:
        days = 90

    try:
        from product.signals.gmail import extract_threads_for_contact
    except Exception as exc:
        logger.warning("[chat-tools] find_threads_with_contact import failed: %s", exc)
        return {
            "contact_email": contact_email,
            "threads": [],
            "error": f"import_failed: {exc}",
        }

    try:
        threads = await asyncio.to_thread(
            extract_threads_for_contact, csm_id, contact_email, days
        )
    except Exception as exc:
        logger.warning(
            "[chat-tools] find_threads_with_contact failed for %s contact=%r: %s",
            csm_id, contact_email, exc,
        )
        return {
            "contact_email": contact_email,
            "threads": [],
            "error": str(exc)[:200],
        }

    return {"contact_email": contact_email, "threads": threads or []}


async def _tool_get_account_dossier(input: dict, csm_id: str) -> dict:
    """Implementation of the get_account_dossier tool.

    Opens an async batch session against HubSpot's MCP server, resolves the
    account name to a hubspot_id, then runs four parallel fetches
    (contacts, deals, tickets, recent_activity) via asyncio.gather with
    return_exceptions=True for partial-failure tolerance. A failure of any
    single fetch is logged and substituted with an empty list, so the LLM
    still gets the data that did succeed.

    Returns the full dossier dict with account, contacts, deals, tickets,
    recent_activity sections, or {"error": "account_not_found"} when the
    name does not match a HubSpot company, or {"error": "hubspot_not_connected"}
    when no token exists.
    """
    raw_name = input.get("account_name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return {"account_name": "", "error": "missing_or_invalid_account_name"}
    account_name = raw_name.strip()

    try:
        from product.connectors.hubspot_mcp import load_hubspot_token
        from product.connectors.hubspot_crm import HubSpotCRMConnector
    except Exception as exc:
        logger.warning("[chat-tools] get_account_dossier import failed: %s", exc)
        return {"account_name": account_name, "error": f"import_failed: {exc}"}

    try:
        token = await asyncio.to_thread(load_hubspot_token, csm_id)
        if not token:
            return {"account_name": account_name, "error": "hubspot_not_connected"}

        connector = HubSpotCRMConnector(csm_id)

        async with connector.batch():
            accounts = await connector._get_accounts_async()
            target = None
            lower_query = account_name.lower()
            for acct in accounts or []:
                if (acct.get("name") or "").lower() == lower_query:
                    target = acct
                    break
            if target is None:
                for acct in accounts or []:
                    if lower_query in (acct.get("name") or "").lower():
                        target = acct
                        break

            if target is None:
                return {"account_name": account_name, "error": "account_not_found"}

            hubspot_id = target.get("hubspot_id")
            if hubspot_id is None:
                return {"account_name": account_name, "error": "account_missing_hubspot_id"}

            contacts, deals, tickets, activity = await asyncio.gather(
                connector._get_contacts_by_id_async(hubspot_id),
                connector._get_deals_for_company_async(hubspot_id),
                connector._get_tickets_for_company_async(hubspot_id),
                connector._get_recent_activity_async(hubspot_id, 14),
                return_exceptions=True,
            )

            from product.connectors.source_status import SourceStatus

            def _normalize(label: str, value) -> tuple[list, str]:
                """Unpack (items, status) tuple, bare list, or Exception into
                (data, status_str). Exceptions become "failed". Bare lists
                (legacy: _get_recent_activity_async) derive status from
                emptiness since the connector method has no status channel
                yet; this is tracked as a Sprint 6 follow-up.
                """
                if isinstance(value, Exception):
                    logger.warning(
                        "[chat-tools] get_account_dossier %s fetch failed for %s: %s",
                        label, account_name, value,
                    )
                    return ([], SourceStatus.FAILED.value)
                if isinstance(value, tuple) and len(value) == 2:
                    items, status = value
                    items = items or []
                    status_str = status.value if hasattr(status, "value") else str(status)
                    return (items, status_str)
                items = value or []
                return (
                    items,
                    SourceStatus.OK.value if items else SourceStatus.EMPTY.value,
                )

            contacts_data, contacts_status = _normalize("contacts", contacts)
            deals_data, deals_status = _normalize("deals", deals)
            tickets_data, tickets_status = _normalize("tickets", tickets)
            activity_data, activity_status = _normalize("recent_activity", activity)

            return {
                "account": {
                    "name": target.get("name", account_name),
                    "id": hubspot_id,
                    "domain": target.get("domain", ""),
                    "industry": target.get("industry", ""),
                    "owner_id": target.get("owner_id", ""),
                },
                "contacts": contacts_data,
                "deals": deals_data,
                "tickets": tickets_data,
                "recent_activity": activity_data,
                "_status": {
                    "contacts": contacts_status,
                    "deals": deals_status,
                    "tickets": tickets_status,
                    "recent_activity": activity_status,
                },
            }
    except Exception as exc:
        logger.warning(
            "[chat-tools] get_account_dossier failed for %s account=%r: %s",
            csm_id, account_name, exc,
        )
        return {"account_name": account_name, "error": str(exc)[:200]}


async def _tool_find_recent_transcripts(input: dict, csm_id: str) -> dict:
    """Implementation of the find_recent_transcripts tool.

    Wraps find_meet_transcripts from product/signals/drive.py. The
    underlying function returns dicts with `title` and `content_snippet`
    keys; this wrapper renames them to `name` and `snippet` to match
    the tool's declared output schema.

    Optional `query` filters the result list by case-insensitive
    substring match against the transcript name. days is clamped to
    [1, 60].
    """
    raw_query = input.get("query")
    query = raw_query.strip() if isinstance(raw_query, str) and raw_query.strip() else None

    raw_days = input.get("days", 14)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = 14
    if days < 1:
        days = 1
    if days > 60:
        days = 60

    try:
        from product.signals.drive import find_meet_transcripts
    except Exception as exc:
        logger.warning("[chat-tools] find_recent_transcripts import failed: %s", exc)
        return {"transcripts": [], "error": f"import_failed: {exc}"}

    try:
        raw = await find_meet_transcripts(csm_id, days)
    except Exception as exc:
        logger.warning(
            "[chat-tools] find_recent_transcripts failed for %s days=%d: %s",
            csm_id, days, exc,
        )
        return {"transcripts": [], "error": str(exc)[:200]}

    transcripts: list[dict] = []
    q_lower = query.lower() if query else None
    for t in raw or []:
        name = t.get("title", "") or ""
        if q_lower and q_lower not in name.lower():
            continue
        transcripts.append({
            "file_id": t.get("file_id", ""),
            "name": name,
            "modified_time": t.get("modified_time", ""),
            "created_time": t.get("created_time", ""),
            "snippet": t.get("content_snippet", "") or "",
            "web_link": t.get("web_link", ""),
        })

    return {"transcripts": transcripts}


async def _tool_read_transcript_excerpt(input: dict, csm_id: str) -> dict:
    """Implementation of the read_transcript_excerpt tool.

    Wraps fetch_transcript_body from product/signals/drive.py. Returns
    up to max_chars of the doc body. When truncated, appends a marker
    so the LLM can recognize the cutoff and ask for more if needed.

    On any extractor exception, returns the empty content shape with
    an error string so the LLM can recover via tool_result.
    """
    raw_file_id = input.get("file_id")
    if not isinstance(raw_file_id, str) or not raw_file_id.strip():
        return {
            "file_id": raw_file_id if isinstance(raw_file_id, str) else "",
            "content": "",
            "error": "missing_or_invalid_file_id",
        }
    file_id = raw_file_id.strip()

    raw_max = input.get("max_chars", 4000)
    try:
        max_chars = int(raw_max)
    except (TypeError, ValueError):
        max_chars = 4000
    if max_chars < 500:
        max_chars = 500
    if max_chars > 8000:
        max_chars = 8000

    try:
        from product.signals.drive import fetch_transcript_body
    except Exception as exc:
        logger.warning("[chat-tools] read_transcript_excerpt import failed: %s", exc)
        return {"file_id": file_id, "content": "", "error": f"import_failed: {exc}"}

    try:
        body = await asyncio.to_thread(fetch_transcript_body, csm_id, file_id)
    except Exception as exc:
        logger.warning(
            "[chat-tools] read_transcript_excerpt failed for %s file_id=%s: %s",
            csm_id, file_id, exc,
        )
        return {"file_id": file_id, "content": "", "error": str(exc)[:200]}

    body = body or ""
    truncated = len(body) > max_chars
    if truncated:
        content = body[:max_chars] + "... [truncated]"
    else:
        content = body

    return {
        "file_id": file_id,
        "content": content,
        "truncated": truncated,
        "char_count": len(content),
    }


async def _tool_slack_draft_message(input: dict, csm_id: str) -> dict:
    """Implementation of the slack_draft_message tool.

    Pure preview. This tool never calls the Slack API and never sends
    anything - it echoes back a normalized draft so the LLM can emit a
    <<<DRAFT_CARD>>> with action_type "slack_message". The actual send is
    the approve-triggered /app/slack/send endpoint, gated on CSM approval
    of the draft card. This preserves the registry invariant that tools
    never write.

    Never raises: on bad input returns an empty-shape dict with an error
    key so the LLM can recover via tool_result.
    """
    try:
        raw_recipient = input.get("recipient")
        raw_message = input.get("message")
        recipient = raw_recipient.strip() if isinstance(raw_recipient, str) else ""
        message = raw_message.strip() if isinstance(raw_message, str) else ""
        raw_account = input.get("account_context")
        account_context = raw_account.strip() if isinstance(raw_account, str) and raw_account.strip() else ""

        if not recipient or not message:
            return {
                "error": "missing_recipient_or_message",
                "recipient": "",
                "message": "",
                "account_context": "",
            }

        return {
            "recipient": recipient,
            "message": message,
            "account_context": account_context,
        }
    except Exception as exc:
        logger.warning("[chat-tools] slack_draft_message failed for %s: %s", csm_id, exc)
        return {
            "error": str(exc)[:200],
            "recipient": "",
            "message": "",
            "account_context": "",
        }
