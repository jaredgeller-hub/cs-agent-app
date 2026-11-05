"""
Org-level multi-CSM configuration system.

Loads org_config.json from the repo root. Falls back to pilot_config.json
(single-CSM) and wraps it in org_config format for backward compatibility.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("cs-agent.org_config")

_REPO_ROOT = Path(__file__).parent.parent
_ORG_CONFIG_PATH = _REPO_ROOT / "org_config.json"
_PILOT_CONFIG_PATH = _REPO_ROOT / "pilot_config.json"

_REQUIRED_FIELDS = {"org_name", "crm_type", "timezone", "csms"}

_cached_config: dict | None = None


class OrgConfigMissingError(FileNotFoundError):
    """Neither org_config.json nor pilot_config.json exists.

    Subclasses FileNotFoundError so existing callers that catch
    FileNotFoundError (or Exception) behave exactly as before. Callers that
    want to treat "no config file" as "no configured CSMs" catch this class
    specifically, so a malformed or unreadable file is never mistaken for
    an absent one.
    """


def _resolve_credentials(value):
    """Recursively resolve {"env": "VAR_NAME"} references to env var values."""
    if isinstance(value, dict):
        if set(value.keys()) == {"env"}:
            env_var = value["env"]
            return os.environ.get(env_var, "")
        return {k: _resolve_credentials(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_credentials(item) for item in value]
    return value


def _wrap_pilot_config(pilot: dict) -> dict:
    """Wrap a single-CSM pilot_config.json into org_config format."""
    csm_id = pilot.get("csm_id", "sarah_c")
    return {
        "org_name": "Pilot Org",
        "crm_type": pilot.get("crm", "salesforce"),
        "crm_credentials": {"env": pilot.get("crm_api_key_env", "PLANHAT_API_KEY")},
        "secondary_crm_type": None,
        "secondary_crm_credentials": None,
        "timezone": pilot.get("timezone", "America/Chicago"),
        "digest_time": pilot.get("digest_time", "08:00"),
        "absence_check_times": ["08:00", "12:00", "16:00"],
        "csms": {
            csm_id: {
                "name": pilot.get("name", csm_id),
                "email": pilot.get("email", ""),
                "accounts": "auto",
                "notifications": {
                    "email": pilot.get("email_notifications", True)
                },
            }
        },
        "_source": "pilot_config",
    }


def load_org_config(force_reload: bool = False) -> dict:
    """
    Load org config from org_config.json. Falls back to pilot_config.json.

    Resolves credential env var references in-place.
    Validates required fields: org_name, crm_type, timezone, csms.
    Caches the result; pass force_reload=True to bust the cache.
    """
    global _cached_config
    if _cached_config is not None and not force_reload:
        return _cached_config

    if _ORG_CONFIG_PATH.exists():
        with open(_ORG_CONFIG_PATH) as f:
            config = json.load(f)
        source = "org_config"
    elif _PILOT_CONFIG_PATH.exists():
        with open(_PILOT_CONFIG_PATH) as f:
            pilot = json.load(f)
        config = _wrap_pilot_config(pilot)
        source = "pilot_config (fallback)"
    else:
        raise OrgConfigMissingError(
            "Neither org_config.json nor pilot_config.json found in repo root."
        )

    missing = _REQUIRED_FIELDS - set(config.keys())
    if missing:
        raise ValueError(f"org_config missing required fields: {missing} (source: {source})")

    if not isinstance(config.get("csms"), dict):
        raise ValueError("org_config.csms must be a dict of csm_id -> csm config")

    # Resolve credential env var references
    for cred_key in ("crm_credentials", "secondary_crm_credentials"):
        if config.get(cred_key):
            config[cred_key] = _resolve_credentials(config[cred_key])

    _cached_config = config
    return config


def get_csm_config(csm_id: str) -> dict:
    """
    Return config for a specific CSM.
    Raises KeyError if csm_id is not found.
    """
    config = load_org_config()
    csms = config["csms"]
    if csm_id not in csms:
        raise KeyError(f"CSM '{csm_id}' not found in org config. Available: {list(csms)}")
    return csms[csm_id]


def try_get_csm_config(csm_id: str) -> dict | None:
    """
    Non-raising variant of get_csm_config. Returns the org_config entry
    for csm_id if present, None otherwise. Use this when org_config
    absence is an expected control-flow signal (Clerk users not in the
    demo registry) rather than an error.

    No config file at all (fresh self-hosted install) also returns None:
    no CSM is configured. A config file that exists but is malformed still
    raises.
    """
    try:
        return get_csm_config(csm_id)
    except (KeyError, OrgConfigMissingError):
        return None


def list_active_csms() -> list[str]:
    """Return list of all csm_id keys from org_config.csms."""
    config = load_org_config()
    return list(config["csms"].keys())


def get_pilot_mode() -> str:
    """Return 'shadow' or 'live'. Defaults to 'live' if not set, including
    when there is no config file at all (fresh self-hosted install)."""
    try:
        config = load_org_config()
    except OrgConfigMissingError:
        return "live"
    return config.get("pilot_mode", "live")


def get_crm_config() -> dict:
    """
    Return org-level CRM config with resolved credentials.

    Keys: crm_type, credentials, secondary_crm_type, secondary_crm_credentials.
    """
    config = load_org_config()
    return {
        "crm_type": config.get("crm_type"),
        "credentials": config.get("crm_credentials"),
        "secondary_crm_type": config.get("secondary_crm_type"),
        "secondary_crm_credentials": config.get("secondary_crm_credentials"),
    }


# ---------------------------------------------------------------------------
# CSMConfig: unified view across org_config.json and the customers table
# ---------------------------------------------------------------------------

@dataclass
class CSMConfig:
    csm_id: str
    name: str
    email: str
    timezone: str
    accounts: list[str]
    notifications_email: bool
    pilot_mode: str
    source: str  # "org_config" or "customer_row"


def _coerce_accounts(raw_accounts, *, csm_id: str, source: str) -> list[str]:
    """Normalize the accounts field to list[str].

    None and the legacy "auto" sentinel both mean "no accounts" silently.
    Any other non-list shape is a data integrity issue (F11/F12 class) —
    log a WARNING with enough detail for log-only triage, then return [].
    """
    if isinstance(raw_accounts, list):
        return raw_accounts
    if raw_accounts is None or raw_accounts == "auto":
        return []
    logger.warning(
        "malformed accounts shape",
        extra={
            "csm_id": csm_id,
            "source": source,
            "type": type(raw_accounts).__name__,
            "value_preview": str(raw_accounts)[:200],
        },
    )
    return []


def resolve_csm_config(csm_id: str) -> CSMConfig | None:
    """
    Return a unified CSMConfig view for csm_id.

    Precedence: org_config.json wins over the customers table. The richer
    per-user overrides take priority. Only fall through to the Customer-row
    lookup if csm_id is not in org_config.

    Returns None if csm_id is in neither. DB-unavailability is not caught
    here — it propagates so the broader system (cron error surfacing,
    structured logs, health checks) can see it.
    """
    org = load_org_config()
    csms = org.get("csms", {})

    if csm_id in csms:
        entry = csms[csm_id]
        notifications = entry.get("notifications") or {}
        return CSMConfig(
            csm_id=csm_id,
            name=entry.get("name", ""),
            email=entry.get("email", ""),
            timezone=entry.get("timezone", org.get("timezone", "America/Chicago")),
            accounts=_coerce_accounts(
                entry.get("accounts"), csm_id=csm_id, source="org_config"
            ),
            notifications_email=bool(notifications.get("email", True)),
            pilot_mode=entry.get("pilot_mode", org.get("pilot_mode", "live")),
            source="org_config",
        )

    # Fall through to Customer row lookup. DB import + SessionLocal
    # access are intentionally not wrapped in try/except — fail loudly
    # if the DB is unavailable so the cron job / health checks notice.
    from product.database import Customer, SessionLocal

    session = SessionLocal()
    try:
        customer = session.query(Customer).filter_by(clerk_user_id=csm_id).first()
        if customer is None:
            return None
        return CSMConfig(
            csm_id=csm_id,
            name=customer.name or "",
            email=customer.email or "",
            timezone=customer.timezone or org.get("timezone", "America/Chicago"),
            accounts=_coerce_accounts(
                customer.accounts, csm_id=csm_id, source="customer_row"
            ),
            notifications_email=True,  # no column; default on
            pilot_mode=org.get("pilot_mode", "live"),
            source="customer_row",
        )
    finally:
        session.close()


def list_proactive_eligible_csms() -> list[str]:
    """
    Return clerk_user_ids of customers eligible for proactive alerts.

    Eligible set: subscription_status IN ('trialing', 'active')
    AND clerk_user_id IS NOT NULL AND is_deleted IS FALSE.

    past_due is intentionally excluded: is_subscription_active() in
    product/subscription.py gates past_due users out of the app, so they
    cannot see or act on proactive alerts. Generating alerts for them
    wastes CRM/LLM compute and diverges from the access gate. Keep this
    set in sync with is_subscription_active's "has access" predicate.

    Sarah and Reviewer Test exist as Customer rows (Phase 0d), so a union
    with list_active_csms() is not required. DB-unavailability is not
    caught here — it propagates so Phase 4's narrow OperationalError
    handler can fail the F1 cron loudly instead of fanning out to zero
    CSMs silently.
    """
    from product.database import Customer, SessionLocal

    session = SessionLocal()
    try:
        rows = (
            session.query(Customer.clerk_user_id)
            .filter(
                # past_due omitted: those users are gated from the app by
                # is_subscription_active and cannot act on alerts.
                Customer.subscription_status.in_(("trialing", "active")),
                Customer.clerk_user_id.isnot(None),
                # I.2: soft-deleted (orphaned/abandoned) rows never fan out.
                Customer.is_deleted.is_(False),
            )
            .all()
        )
        return [r[0] for r in rows]
    finally:
        session.close()


def list_all_csms_with_connections() -> list[str]:
    """
    Return clerk_user_ids of every user with at least one row in the
    connections table — unfiltered by subscription_status.

    A user who connected a provider is owed accurate connection state
    regardless of whether they are trialing, past_due, or canceled (data
    portability principle). Used by the eager connection-state probe
    scheduler. Distinct from list_proactive_eligible_csms (which gates
    proactive alerts behind active billing).

    Connection.customer_id holds either a Clerk user_id or a Customer.id
    depending on the write path (database.py:92-93 schema comment). This
    helper maps Customer.id rows to their clerk_user_id; rows whose
    customer_id is not a Customer.id are returned as-is.

    Returns deduped, sorted list. Empty list if connections table is empty.
    DB-unavailability is not caught here; the caller decides whether the
    probe fan-out should fail loudly or fan out to zero.
    """
    from product.database import Connection, Customer, SessionLocal

    session = SessionLocal()
    try:
        raw_customer_ids = {
            row[0]
            for row in session.query(Connection.customer_id).distinct().all()
            if row[0]
        }
        if not raw_customer_ids:
            return []

        # Resolve any Customer.id values to their clerk_user_id.
        customers_by_internal_id = {
            c.id: c.clerk_user_id
            for c in session.query(Customer)
            .filter(Customer.id.in_(raw_customer_ids))
            .all()
            if c.clerk_user_id
        }

        resolved: set[str] = set()
        for cid in raw_customer_ids:
            mapped = customers_by_internal_id.get(cid)
            resolved.add(mapped if mapped else cid)
        return sorted(resolved)
    finally:
        session.close()
