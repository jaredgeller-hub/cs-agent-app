"""Authoritative connection state service.

Owns the answer to "is this user's connection to this provider healthy"
across the codebase. Every reader (/pilot/health, auth_probe consumers,
digest pipeline, token expiry UX, future Sprint 5 fan-out) calls through
this service rather than implementing its own connection-health logic.

The service is READ-ONLY. Token writes stay in OAuth callbacks. The
service does not make live API calls; all reads are from local stores.

This module defines the public contract. Step 2 implements against
Postgres.

Track L scoping doc:
https://www.notion.so/3591d2f9aff081a087d5d0410fe862f2
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Iterable

logger = logging.getLogger(__name__)


_GOOGLE_DB_PROVIDERS = ("google", "gmail", "calendar")


@dataclass
class ConnectionState:
    """Authoritative state for one (csm_id, provider) pair.

    Returned by get_connection_state and list_active_connections. All
    fields are computed at read time from local stores; the service does
    not make live API calls.

    Field notes:
        provider: Canonical provider name. Accepted values are "google",
            "hubspot", "gainsight". The Google OAuth flow writes rows
            under both Connection.provider="google" (via
            auth_google.save_token) and Connection.provider="gmail" (via
            the OAuth callback in main.py). Step 2 implementation merges
            both row shapes when a caller asks for "google" and reports
            the result with provider="google" regardless of which row
            supplied the data. Sprint 6 cleanup retires the "gmail"
            writer.

        last_verified_at: Timestamp of the most recent probe of this
            connection (any outcome other than "missing" is recorded).
            Populated by product/connection_writer.write_connection_state
            via the eager hourly probe scheduler and the post-deploy
            startup hook. None when no Connection row has been probed yet
            for this provider.

        last_verified_age_minutes: Convenience derivation of
            last_verified_at. None when last_verified_at is None.

        token_appears_expired: Best-effort shape check on token_data.
            Does NOT make a live API call to confirm expiry. Callers who
            need ground truth should attempt the API call themselves.

        diagnostic_notes: Human-readable notes for debugging. Examples
            include "loaded from filesystem fallback" or "no row in
            Connection table for this provider".
    """

    csm_id: str
    provider: str
    is_healthy: bool
    has_token: bool
    granted_scopes: list[str]
    last_verified_at: datetime | None
    last_verified_age_minutes: int | None
    token_appears_expired: bool
    diagnostic_notes: list[str]


def get_connection_state(csm_id: str, provider: str) -> ConnectionState:
    """Authoritative connection state for this user and provider.

    Reads from Postgres. Falls back to filesystem JSON for legacy users
    until the deprecation cleanup completes (Sprint 7). Returns a
    structured result; never raises.

    The provider argument accepts canonical names ("google", "hubspot",
    "gainsight"). For the Google connection, both Connection.provider=
    "google" and Connection.provider="gmail" rows are looked up and
    merged, since the OAuth callback writes both shapes. The returned
    ConnectionState always reports provider="google" regardless of which
    row supplied the data.

    Step 1 stub. Implementation lands in Step 2.
    """
    canonical = provider
    session = None
    rows: list = []
    fallback_token_data: dict | None = None
    try:
        from product.database import SessionLocal
        if SessionLocal is None:
            return ConnectionState(
                csm_id=csm_id,
                provider=canonical,
                is_healthy=False,
                has_token=False,
                granted_scopes=[],
                last_verified_at=None,
                last_verified_age_minutes=None,
                token_appears_expired=False,
                diagnostic_notes=["database session unavailable"],
            )
        session = SessionLocal()
        customer_keys = _resolve_customer_keys(session, csm_id)
        rows = _load_provider_rows(session, customer_keys, canonical)
    except Exception as exc:
        logger.warning("get_connection_state DB read failed for %s/%s: %s", csm_id, canonical, exc)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    if canonical == "google" and not rows:
        try:
            from product.auth_google import load_google_token
            fallback_token_data = load_google_token(csm_id)
        except Exception as exc:
            logger.warning("Google filesystem fallback failed for %s: %s", csm_id, exc)
            fallback_token_data = None

    return _state_from_rows(csm_id, canonical, rows, fallback_token_data)


def list_active_connections(csm_id: str) -> list[ConnectionState]:
    """All connection states for a user, one per canonical provider that
    has any data in Postgres or the filesystem fallback.

    Returns an empty list (not None) for a user with zero connections.
    Never raises.

    Step 1 stub. Implementation lands in Step 2.
    """
    states: list[ConnectionState] = []
    for canonical in ("google", "hubspot", "gainsight"):
        state = get_connection_state(csm_id, canonical)
        if state.has_token:
            states.append(state)
    return states


def has_scope(csm_id: str, provider: str, scope: str) -> bool:
    """Convenience: did this user grant this specific scope to this provider.

    Used by /pilot/health to check has_scope(csm_id, "google", "calendar")
    and similar callers in Sprint 6 migrations. Returns False if the
    connection does not exist or the scope was not granted. Never raises.

    Step 1 stub. Implementation lands in Step 2.
    """
    state = get_connection_state(csm_id, provider)
    return scope in state.granted_scopes


def _resolve_customer_keys(session, csm_id: str) -> list[str]:
    """Return the list of customer_id values to query against Connection rows.

    Connection.customer_id holds either a Clerk user_id or a Customer.id
    depending on the write path (per database.py schema comment). Lookups
    must try both. We start with csm_id as-is (typically the Clerk user_id),
    then reverse-lookup the matching Customer.id via
    Customer.clerk_user_id == csm_id.
    """
    keys: list[str] = [csm_id]
    try:
        from product.database import Customer
        cust = session.query(Customer).filter_by(clerk_user_id=csm_id).first()
        if cust and cust.id and cust.id != csm_id:
            keys.append(cust.id)
    except Exception as exc:
        logger.warning("Customer reverse-lookup failed for %s: %s", csm_id, exc)
    return keys


def _load_provider_rows(
    session, customer_keys: list[str], canonical_provider: str
) -> list:
    """Return all Connection rows under the canonical provider for any
    customer key, including aliased rows.

    For canonical_provider == "google", returns rows under both
    provider="google" and provider="gmail" (the OAuth callback writes
    both). For "hubspot" and "gainsight", returns only the matching
    provider rows.
    """
    try:
        from product.database import Connection
        if canonical_provider == "google":
            provider_filters = ["google", "gmail"]
        else:
            provider_filters = [canonical_provider]
        rows = (
            session.query(Connection)
            .filter(Connection.customer_id.in_(customer_keys))
            .filter(Connection.provider.in_(provider_filters))
            .all()
        )
        return list(rows)
    except Exception as exc:
        logger.warning(
            "Connection row load failed for keys=%s provider=%s: %s",
            customer_keys, canonical_provider, exc,
        )
        return []


def _decode_row_token(row) -> dict:
    """Unwrap a Connection row's token_data for the shape-only helpers.

    Stage 2a encryption persists token_data as {"ciphertext": "gAAAAA..."}.
    This decoder decrypts that shape so _extract_scopes_from_token and
    _token_appears_expired keep receiving the original dict shapes they
    were written against. Legacy plaintext rows are returned as-is during
    the dual-read window.

    Returns {} on missing row, missing token_data, decrypt failure, or
    unexpected error. Decrypt failures are logged with the customer_id
    and provider so the founder-alert wiring can surface them.
    """
    if row is None or not row.token_data:
        return {}
    try:
        raw = dict(row.token_data)
    except Exception:
        return {}

    if (
        set(raw.keys()) == {"ciphertext"}
        and isinstance(raw.get("ciphertext"), str)
        and raw["ciphertext"].startswith("gAAAAA")
    ):
        try:
            from product.token_crypto import decrypt
            plaintext = decrypt(raw["ciphertext"])
        except Exception as exc:
            logger.warning(
                "token_decrypt_failed for customer_id=%s provider=%s: %s",
                getattr(row, "customer_id", "?"),
                getattr(row, "provider", "?"),
                type(exc).__name__,
            )
            return {}
        try:
            import json
            decoded = json.loads(plaintext)
            return decoded if isinstance(decoded, dict) else {}
        except Exception as exc:
            logger.warning(
                "token_decrypt_format_invalid for customer_id=%s provider=%s: %s",
                getattr(row, "customer_id", "?"),
                getattr(row, "provider", "?"),
                type(exc).__name__,
            )
            return {}

    return raw


def _extract_scopes_from_token(token_data: dict, provider_label: str) -> list[str]:
    """Extract the list of granted scopes from token_data.

    Handles three shapes observed in production:
        google row (credentials_data): token_data["scopes"] is a list[str]
        gmail row (raw OAuth): token_data["scope"] is a space-delimited str
        hubspot/gainsight: no scope field, returns []
    """
    if not token_data:
        return []
    try:
        if provider_label == "gmail":
            scope_str = token_data.get("scope", "") or ""
            return scope_str.split() if scope_str else []
        scopes = token_data.get("scopes", [])
        if isinstance(scopes, list):
            return [str(s) for s in scopes]
        if isinstance(scopes, str):
            return scopes.split()
        return []
    except Exception:
        return []


def _token_appears_expired(token_data: dict, provider_label: str) -> bool:
    """Best-effort shape-only expiry check. No live API calls.

    google credentials_data: build Credentials.from_authorized_user_info
        and read .expired property. Returns False if expiry field absent.
    gmail raw OAuth: skip; the merged google row is the source of truth.
    hubspot/gainsight: token_data["expires_at"] is epoch seconds.
    """
    if not token_data:
        return False
    try:
        if provider_label == "google":
            from google.oauth2.credentials import Credentials
            try:
                creds = Credentials.from_authorized_user_info(token_data)
                return bool(creds.expired)
            except Exception:
                return False
        if provider_label == "gmail":
            return False
        if provider_label in ("hubspot", "gainsight"):
            import time
            expires_at = token_data.get("expires_at", 0)
            try:
                return time.time() >= float(expires_at)
            except (TypeError, ValueError):
                return False
        return False
    except Exception:
        return False


def _state_from_rows(
    csm_id: str,
    canonical_provider: str,
    rows: list,
    fallback_token_data: dict | None = None,
) -> ConnectionState:
    """Build a ConnectionState from zero or more Connection rows.

    For canonical_provider == "google", merges google and gmail rows:
    the google row supplies token shape and freshness (it is always
    at-least-as-fresh as the gmail row, because refresh writes only via
    auth_google.save_token); the granted_scopes field is the union of
    scopes from both rows so a partial-write user (gmail row written,
    google row save failed silently) is not reported as disconnected.
    """
    notes: list[str] = []

    google_row = None
    gmail_row = None
    primary_row = None
    for row in rows:
        if row.provider == "google":
            google_row = row
        elif row.provider == "gmail":
            gmail_row = row
        elif row.provider == canonical_provider:
            primary_row = row

    has_token = False
    granted_scopes: list[str] = []
    token_appears_expired = False
    token_for_shape: dict | None = None
    primary_label_for_shape = canonical_provider

    if canonical_provider == "google":
        google_token = _decode_row_token(google_row)
        gmail_token = _decode_row_token(gmail_row)
        google_scopes = _extract_scopes_from_token(google_token, "google")
        gmail_scopes = _extract_scopes_from_token(gmail_token, "gmail")
        granted_scopes = sorted(set(google_scopes) | set(gmail_scopes))

        if google_token:
            has_token = True
            token_for_shape = google_token
            primary_label_for_shape = "google"
            if gmail_row:
                notes.append("merged google and gmail rows; google row supplied shape")
        elif gmail_token:
            has_token = True
            token_for_shape = gmail_token
            primary_label_for_shape = "gmail"
            notes.append("only gmail row found; google row missing or save failed silently")
        elif fallback_token_data:
            has_token = True
            token_for_shape = dict(fallback_token_data)
            primary_label_for_shape = "google"
            granted_scopes = _extract_scopes_from_token(token_for_shape, "google")
            notes.append("loaded from filesystem fallback")
        else:
            notes.append("no row in Connection table and no filesystem fallback for this provider")
    else:
        primary_token = _decode_row_token(primary_row)
        if primary_token:
            has_token = True
            token_for_shape = primary_token
            granted_scopes = _extract_scopes_from_token(token_for_shape, canonical_provider)
        else:
            notes.append("no row in Connection table for this provider")

    if has_token and token_for_shape is not None:
        token_appears_expired = _token_appears_expired(token_for_shape, primary_label_for_shape)

    is_healthy = has_token and not token_appears_expired

    last_verified_at, last_verified_age_minutes = _freshest_verified(rows)

    return ConnectionState(
        csm_id=csm_id,
        provider=canonical_provider,
        is_healthy=is_healthy,
        has_token=has_token,
        granted_scopes=granted_scopes,
        last_verified_at=last_verified_at,
        last_verified_age_minutes=last_verified_age_minutes,
        token_appears_expired=token_appears_expired,
        diagnostic_notes=notes,
    )


def _freshest_verified(rows: list) -> tuple[datetime | None, int | None]:
    """Return the most recent last_verified_at across rows and its age
    in minutes. Both are None when no row has been probed yet."""
    from product.database import as_utc

    candidates = [as_utc(r.last_verified_at) for r in rows if r is not None and r.last_verified_at]
    if not candidates:
        return None, None
    latest = max(candidates)
    age_minutes = int((datetime.now(timezone.utc) - latest).total_seconds() // 60)
    return latest, age_minutes


def verify_connection_live(csm_id: str, provider: str) -> dict:
    """DB-backed liveness state for one (csm_id, provider) pair.

    Reads Connection.status and Connection.last_verified_at as populated
    by product/connection_writer.write_connection_state. Returns:

        {
            "provider": <input provider name>,
            "status": "active" | "expired" | "missing",
            "last_verified_at": datetime | None,
            "last_verified_age_minutes": int | None,
            "is_live": bool,                      # status == "active"
        }

    Provider names: "gmail", "calendar", "google", "hubspot", "gainsight".
    For the Google family, all of "gmail" / "calendar" / "google" look
    across Connection rows under provider in {"google", "gmail",
    "calendar"} (the writer keeps them aligned) and report the freshest.

    When no matching Connection row exists the result is the "missing"
    sentinel with is_live=False — distinguishable from "expired" so the
    caller can route the UX (reconnect vs. first-time connect).

    Never raises. DB unavailability returns the "missing" sentinel.
    """
    from product.database import Connection, SessionLocal, as_utc

    if SessionLocal is None:
        return _missing_result(provider)

    session = SessionLocal()
    try:
        if provider in _GOOGLE_DB_PROVIDERS:
            db_providers = list(_GOOGLE_DB_PROVIDERS)
        else:
            db_providers = [provider]

        customer_keys = _resolve_customer_keys(session, csm_id)
        rows = (
            session.query(Connection)
            .filter(Connection.customer_id.in_(customer_keys))
            .filter(Connection.provider.in_(db_providers))
            .all()
        )

        if not rows:
            return _missing_result(provider)

        rows_with_ts = [r for r in rows if r.last_verified_at is not None]
        primary = (
            max(rows_with_ts, key=lambda r: as_utc(r.last_verified_at))
            if rows_with_ts else rows[0]
        )

        last_verified_at = as_utc(primary.last_verified_at)
        if last_verified_at is not None:
            age_minutes = int(
                (datetime.now(timezone.utc) - last_verified_at).total_seconds() // 60
            )
        else:
            age_minutes = None

        status = primary.status or "active"
        return {
            "provider": provider,
            "status": status,
            "last_verified_at": last_verified_at,
            "last_verified_age_minutes": age_minutes,
            "is_live": status == "active",
        }
    except Exception as exc:
        logger.warning(
            "event=verify_connection_live_failed csm_id=%s provider=%s error=%s",
            csm_id, provider, exc,
        )
        return _missing_result(provider)
    finally:
        try:
            session.close()
        except Exception:
            pass


def _missing_result(provider: str) -> dict:
    return {
        "provider": provider,
        "status": "missing",
        "last_verified_at": None,
        "last_verified_age_minutes": None,
        "is_live": False,
    }
