"""
product/context_cache.py

Pre-computed CRM context snapshots for the /chat endpoint.
Refreshes pull live data from the user's configured CRM (Gainsight or
HubSpot) and store a JSON summary in Postgres so chat responses can
skip the 10-30s MCP round-trip.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone

logger = logging.getLogger("cs-agent.context-cache")


def _build_summary_text(
    *,
    all_accounts: list[dict],
    deals: list[dict],
    contacts: list[dict],
    tickets: list[dict],
    health_scores: list[dict],
    account_status: dict[str, dict[str, str]] | None = None,
) -> str:
    """Pure builder for the chat system-prompt summary.

    When account_status is provided and any per-account section has status
    "failed", append a "Data fetch issues" line so the LLM can distinguish
    fetch failure from legitimate emptiness. account_status keys are
    stringified hubspot_ids; the function looks up the account name from
    all_accounts to render a human-readable marker.

    account_status of None or {} preserves the pre-commit-2 output exactly,
    which is the legacy / Gainsight backward-compatibility path.
    """
    lines = [f"User manages {len(all_accounts)} accounts."]

    if deals:
        deals_sorted = sorted(deals, key=lambda d: d.get("days_until_close") or 9999)
        deal_strs = []
        for d in deals_sorted[:10]:
            amt = int(d.get("amount") or 0)
            deal_strs.append(
                f"{d.get('account_name')}: {d.get('name')} "
                f"(stage: {d.get('stage')}, ${amt:,}, closes: {d.get('close_date')})"
            )
        lines.append(f"Key deals ({len(deals_sorted)} total): " + "; ".join(deal_strs[:5]))

    acct_summaries = []
    for acct in all_accounts[:15]:
        name = acct.get("name", "")
        acct_contacts_for = [c for c in contacts if c.get("account_name") == name]
        contact_str = ", ".join(
            f"{c.get('name')} ({c.get('role', 'unknown role')})"
            for c in acct_contacts_for[:2]
        ) or "no contacts on file"
        acct_summaries.append(f"{name} (contact: {contact_str})")

    if acct_summaries:
        lines.append("Key accounts: " + "; ".join(acct_summaries[:10]))

    if tickets:
        high_tickets = [t for t in tickets if (t.get("priority") or "").lower() in ("high", "urgent")]
        if high_tickets:
            ticket_strs = [
                f"{t.get('account_name')}: {t.get('subject')} ({t.get('status')})"
                for t in high_tickets[:5]
            ]
            lines.append("High-priority tickets: " + "; ".join(ticket_strs))

    if health_scores:
        at_risk = [
            h for h in health_scores
            if (h.get("trend") == "declining")
            or str(h.get("color_code") or "").lower() in ("red", "yellow")
        ]
        if at_risk:
            health_strs = [
                f"{h.get('account_name')} ({h.get('trend') or h.get('color_code') or '—'})"
                for h in at_risk[:5]
            ]
            lines.append(f"Health at risk ({len(at_risk)}): " + "; ".join(health_strs))

    if account_status:
        id_to_name = {
            str(a.get("hubspot_id")): a.get("name", "")
            for a in all_accounts
            if a.get("hubspot_id") is not None
        }
        failures: list[str] = []
        for acct_key, sections in account_status.items():
            name = id_to_name.get(str(acct_key), "")
            if not name or not isinstance(sections, dict):
                continue
            for section, status in sections.items():
                if status == "failed":
                    failures.append(f"{name} {section} unavailable (fetch failed)")
        if failures:
            lines.append("Data fetch issues: " + "; ".join(failures))

    return " ".join(lines)


async def refresh_user_context(csm_id: str) -> None:
    """Pull CRM data from the user's connected CRM and cache a summary.

    Detects which CRM connector to use based on which token exists in
    token_store (Gainsight first, HubSpot fallback). Saves the result to
    user_context_snapshots (upsert on csm_id). Never raises — this runs in
    a background job.
    """
    from product.database import SessionLocal, UserContextSnapshot
    from product.csm_id_validation import validate_csm_id
    from product.connectors.source_status import SourceStatus
    csm_id = validate_csm_id(csm_id)

    if not SessionLocal:
        logger.warning("[context-cache] DB not configured, skipping refresh for %s", csm_id)
        return

    # Mark as refreshing
    session = SessionLocal()
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = (
            pg_insert(UserContextSnapshot)
            .values(csm_id=csm_id, context_json="{}", refresh_status="refreshing",
                    refreshed_at=datetime.now(timezone.utc))
            .on_conflict_do_update(
                index_elements=["csm_id"],
                set_=dict(refresh_status="refreshing", refreshed_at=datetime.now(timezone.utc)),
            )
        )
        session.execute(stmt)
        session.commit()
    except Exception as exc:
        logger.warning("[context-cache] Could not mark refreshing for %s: %s", csm_id, exc)
        session.rollback()
    finally:
        session.close()

    # Detect which CRM connector to use based on available tokens
    connector = None
    crm_type: str | None = None

    # Check Gainsight first (richer data: health scores, CTAs, success plans)
    try:
        from product.connectors.gainsight_mcp import load_gainsight_token
        gs_token = load_gainsight_token(csm_id)
        if gs_token and gs_token.get("access_token"):
            from product.connectors.gainsight import GainsightCRMConnector
            connector = GainsightCRMConnector(csm_id)
            crm_type = "gainsight"
            logger.info("[context-cache] %s: using Gainsight connector", csm_id)
    except Exception as exc:
        logger.debug("[context-cache] Gainsight token check failed: %s", exc)

    # Fall back to HubSpot
    if connector is None:
        try:
            from product.connectors.hubspot_mcp import load_hubspot_token
            hs_token = load_hubspot_token(csm_id)
            if hs_token and hs_token.get("access_token"):
                from product.connectors.hubspot_crm import HubSpotCRMConnector
                connector = HubSpotCRMConnector(csm_id)
                crm_type = "hubspot"
                logger.info("[context-cache] %s: using HubSpot connector", csm_id)
        except Exception as exc:
            logger.debug("[context-cache] HubSpot token check failed: %s", exc)

    if connector is None:
        _set_status(csm_id, "failed")
        logger.warning(
            "[context-cache] %s: no CRM token found (checked Gainsight, HubSpot)", csm_id
        )
        return

    # Pull CRM data
    try:
        # Load user's onboarding accounts for filtering
        onboarding_accounts: list[str] = []
        try:
            from product.csm_id_validation import safe_data_file
            _acct_file = safe_data_file("product/data/accounts", csm_id)
            if _acct_file.exists():
                _acct_data = json.loads(_acct_file.read_text(encoding="utf-8"))
                onboarding_accounts = _acct_data.get("accounts", [])
        except Exception:
            pass
        if not onboarding_accounts:
            try:
                from product.database import Customer
                _db = SessionLocal()
                try:
                    _c = _db.query(Customer).filter_by(clerk_user_id=csm_id).first()
                    if _c and _c.accounts:
                        onboarding_accounts = _c.accounts
                finally:
                    _db.close()
            except Exception:
                pass

        def _in_scope(name: str) -> bool:
            return not onboarding_accounts or name in onboarding_accounts

        # Renewals / deals closing within 180 days (Gainsight repurposes this
        # bucket for renewals so the downstream summary builder works unchanged).
        deals: list[dict] = []
        today = date.today()
        contacts: list[dict] = []
        tickets: list[dict] = []  # Gainsight repurposes for open CTAs
        health_scores: list[dict] = []
        success_plans: list[dict] = []
        # Per-account fetch status, keyed by HubSpot id (string) for JSON-friendly
        # serialization. Gainsight branch is not threaded in this commit, so the
        # by_account dict will be empty for Gainsight refreshes.
        account_status: dict[str, dict[str, str]] = {}

        # Open one reusable MCP session for the entire CRM pull (perf fix #17)
        async with connector.batch():
            # 1. Accounts
            raw_accounts = await connector._get_accounts_async()
            if crm_type == "hubspot":
                all_accounts = [
                    {"id": a["id"], "hubspot_id": a.get("hubspot_id"), "name": a["name"],
                     "tier": "standard", "owner": a.get("owner_id", "")}
                    for a in raw_accounts
                    if _in_scope(a.get("name", ""))
                ]
            else:  # gainsight
                all_accounts = [
                    {"id": a["id"], "name": a["name"], "tier": "standard",
                     "owner": a.get("owner_id", ""), "status": a.get("status", ""),
                     "arr": a.get("arr"), "mrr": a.get("mrr")}
                    for a in raw_accounts
                    if _in_scope(a.get("name", ""))
                ]
            logger.info("[context-cache] %s: %d accounts loaded (%s)", csm_id, len(all_accounts), crm_type)

            # Per-account detail. HubSpot keys on numeric hubspot_id; Gainsight
            # keys on account name. Cap at 20 to stay within rate limits.
            # Bumped from 10 on 2026-04-27 to support 15-account portfolios
            # (Sarah's expanded demo set) without silently dropping data.
            if crm_type == "hubspot":
                _detail_accounts = [
                    a for a in all_accounts
                    if a.get("hubspot_id") is not None and a.get("name")
                ][:20]
            else:
                _detail_accounts = [a for a in all_accounts if a.get("name")][:20]
            logger.info(
                "[context-cache] %s: fetching detail for %d/%d accounts",
                csm_id, len(_detail_accounts), len(all_accounts),
            )

            for _idx, acct in enumerate(_detail_accounts):
                name = acct["name"]

                # Rate-limit: 0.5s delay between accounts (skip before first)
                if _idx > 0:
                    await asyncio.sleep(0.5)

                if crm_type == "hubspot":
                    hubspot_id = acct["hubspot_id"]
                    acct_key = str(hubspot_id)
                    acct_section_status: dict[str, str] = {}
                    # Contacts
                    try:
                        acct_contacts, _c_status = await connector._get_contacts_by_id_async(int(hubspot_id))
                        acct_section_status["contacts"] = _c_status.value
                        contacts.extend({**c, "account_name": name} for c in acct_contacts[:3])
                    except Exception as _e:
                        acct_section_status["contacts"] = SourceStatus.FAILED.value
                        logger.warning("[context-cache] %s: contacts for %s (id=%s) failed: %s",
                                       csm_id, name, hubspot_id, _e)
                    # Deals
                    try:
                        acct_deals, _d_status = await connector._get_deals_for_company_async(int(hubspot_id))
                        acct_section_status["deals"] = _d_status.value
                        for d in acct_deals[:5]:
                            close_date = (d.get("close_date") or "")[:10]
                            try:
                                days_until = (date.fromisoformat(close_date) - today).days
                                if days_until <= 180:
                                    deals.append({**d, "account_name": name, "days_until_close": days_until})
                            except (ValueError, TypeError) as _parse_err:
                                logger.warning("[context-cache] %s: failed to parse close_date for deal %s: %s",
                                               csm_id, d.get("name", "?"), _parse_err)
                                deals.append({**d, "account_name": name, "days_until_close": None})
                    except Exception as _e:
                        acct_section_status["deals"] = SourceStatus.FAILED.value
                        logger.warning("[context-cache] %s: deals for %s (id=%s) failed: %s",
                                       csm_id, name, hubspot_id, _e)
                    # Tickets
                    try:
                        acct_tickets, _t_status = await connector._get_tickets_for_company_async(int(hubspot_id))
                        acct_section_status["tickets"] = _t_status.value
                        tickets.extend({**t, "account_name": name} for t in acct_tickets[:5])
                    except Exception as _e:
                        acct_section_status["tickets"] = SourceStatus.FAILED.value
                        logger.warning("[context-cache] %s: tickets for %s (id=%s) failed: %s",
                                       csm_id, name, hubspot_id, _e)
                    account_status[acct_key] = acct_section_status
                else:  # gainsight — per-account pulls are contacts + success plans
                    try:
                        acct_contacts = (await connector._get_contacts_async(name))[:3]
                        contacts.extend({**c, "account_name": name} for c in acct_contacts)
                    except Exception as _e:
                        logger.debug("[context-cache] %s: contacts for %s failed: %s", csm_id, name, _e)
                    try:
                        acct_plans = await connector._get_success_plans_async(name)
                        success_plans.extend({**p, "account_name": name} for p in acct_plans)
                    except Exception as _e:
                        logger.debug("[context-cache] %s: success plans for %s failed: %s", csm_id, name, _e)

            # Gainsight global queries (single calls, no rate-limit concern)
            if crm_type == "gainsight":
                _scope_names = {a.get("name", "") for a in all_accounts}
                try:
                    raw_renewals = await connector._get_renewals_async()
                    for r in raw_renewals:
                        acct_name = r.get("account_name", "")
                        if _scope_names and acct_name not in _scope_names:
                            continue
                        days_until = r.get("days_until_renewal")
                        if days_until is not None and days_until > 180:
                            continue
                        deals.append({
                            "account_name":     acct_name,
                            "name":             "Renewal",
                            "stage":            "—",
                            "amount":           r.get("contract_value"),
                            "close_date":       r.get("renewal_date", ""),
                            "days_until_close": days_until,
                        })
                except Exception as _e:
                    logger.debug("[context-cache] %s: renewals failed: %s", csm_id, _e)
                try:
                    raw_ctas = await connector._get_open_ctas_async()
                    for c in raw_ctas:
                        acct_name = c.get("account_name", "")
                        if _scope_names and acct_name not in _scope_names:
                            continue
                        tickets.append({
                            "account_name": acct_name,
                            "subject":      c.get("title", ""),
                            "status":       c.get("status", ""),
                            "priority":     c.get("priority", "medium"),
                            "cta_type":     c.get("cta_type", ""),
                        })
                except Exception as _e:
                    logger.debug("[context-cache] %s: CTAs failed: %s", csm_id, _e)
                try:
                    raw_health = await connector._get_health_scores_async()
                    health_scores = [
                        h for h in raw_health
                        if not _scope_names or h.get("account_name", "") in _scope_names
                    ]
                except Exception as _e:
                    logger.debug("[context-cache] %s: health scores failed: %s", csm_id, _e)

        # Build summary text suitable for system prompt injection
        summary_text = _build_summary_text(
            all_accounts=all_accounts,
            deals=deals,
            contacts=contacts,
            tickets=tickets,
            health_scores=health_scores,
            account_status=account_status,
        )

        snapshot = {
            "crm_type": crm_type,
            "accounts": all_accounts,
            "deals": deals[:20],
            "contacts": contacts[:60],
            "tickets": tickets[:20],
            "health_scores": health_scores[:20],
            "success_plans": success_plans[:20],
            "summary_text": summary_text,
            "source_status": {"by_account": account_status},
        }

        # Save to DB
        session = SessionLocal()
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = (
                pg_insert(UserContextSnapshot)
                .values(
                    csm_id=csm_id,
                    context_json=json.dumps(snapshot),
                    refresh_status="complete",
                    refreshed_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_update(
                    index_elements=["csm_id"],
                    set_=dict(
                        context_json=json.dumps(snapshot),
                        refresh_status="complete",
                        refreshed_at=datetime.now(timezone.utc),
                    ),
                )
            )
            session.execute(stmt)
            session.commit()
        finally:
            session.close()

        logger.info(
            "[context-cache] Snapshot refreshed for %s (%s) — %d accounts, %d deals/renewals, "
            "%d contacts, %d tickets/CTAs, %d health, %d success plans",
            csm_id, crm_type, len(all_accounts), len(deals), len(contacts),
            len(tickets), len(health_scores), len(success_plans),
        )

    except Exception as exc:
        _set_status(csm_id, "failed")
        logger.warning("[context-cache] Refresh failed for %s: %s", csm_id, exc)


def _set_status(csm_id: str, status: str) -> None:
    """Update refresh_status for a snapshot row."""
    from product.database import SessionLocal, UserContextSnapshot
    if not SessionLocal:
        return
    session = SessionLocal()
    try:
        row = session.query(UserContextSnapshot).filter_by(csm_id=csm_id).first()
        if row:
            row.refresh_status = status
            row.refreshed_at = datetime.now(timezone.utc)
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def get_cached_context(csm_id: str) -> dict | None:
    """Read the cached context snapshot from Postgres. Returns None if no snapshot exists.

    The returned dict carries source_status as a top-level key (mirrored from
    data["source_status"]) so consumers can inspect per-account fetch outcomes
    without restructuring data. Legacy snapshots written before commit 1 of
    Sprint 5 lack the field; in that case source_status is an empty dict.
    """
    from product.database import SessionLocal, UserContextSnapshot
    if not SessionLocal:
        return None
    session = SessionLocal()
    try:
        row = session.query(UserContextSnapshot).filter_by(csm_id=csm_id).first()
        if row and row.context_json and row.refresh_status == "complete":
            data = json.loads(row.context_json)
            source_status = data.get("source_status") or {}
            return {
                "data": data,
                "refreshed_at": row.refreshed_at,
                "refresh_status": row.refresh_status,
                "source_status": source_status,
            }
        return None
    except Exception:
        return None
    finally:
        session.close()


def get_cached_context_text(csm_id: str) -> str:
    """Read just the summary_text from the cached snapshot. Returns empty string if no snapshot."""
    cached = get_cached_context(csm_id)
    if not cached:
        return ""
    data = cached.get("data", {})
    return data.get("summary_text", "")
