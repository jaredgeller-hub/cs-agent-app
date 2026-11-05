"""
product/connectors/zoom_mcp.py

Zoom OAuth 2.1 with PKCE for the per-user Zoom token, used against the
Zoom Remote MCP Server (https://mcp-us.zoom.us) for transcript retrieval.

Mirrors hubspot_mcp.py (Zoom issues refresh tokens, so the token shape
matches HubSpot's: access_token + refresh_token + expires_at, with a
token_lock_async double-check on refresh). Two Zoom-specific additions:

  1. exchange_zoom_code fetches /v2/users/me after the token exchange and
     stores the Zoom user_id in the token dict (zoom_user_id). This is the
     key the recording.completed webhook reverse-maps from host_id.
  2. lookup_csm_id_by_zoom_user_id scans the stored "zoom" connections to
     resolve a webhook host_id back to a csm_id.

The demo account (sarah_c) must never connect Zoom.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import requests

from product.settings import public_url

logger = logging.getLogger("cs-agent.zoom_mcp")

_ZOOM_AUTH_URL = "https://zoom.us/oauth/authorize"
_ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
_ZOOM_USERS_ME_URL = "https://api.zoom.us/v2/users/me"
# Runtime default only; the redirect URIs registered with the provider
# are unchanged. Set PUBLIC_BASE_URL (or ZOOM_REDIRECT_URI) per deploy.
_DEFAULT_REDIRECT_URI = public_url("/auth/zoom/callback")

# Granular scopes for the two capabilities in this build:
#   meeting:read:search                          - search_meetings
#   meeting:read:assets                           - get_meeting_assets (transcript)
#   cloud_recording:read:list_user_recordings     - recordings_list
#   cloud_recording:read:content                  - get_recording_resource
_DEFAULT_SCOPES = (
    "meeting:read:search "
    "meeting:read:assets "
    "cloud_recording:read:list_user_recordings "
    "cloud_recording:read:content"
)

# Refresh window mirrors hubspot_crm.py: refresh when within 60s of expiry.
_REFRESH_BUFFER_S = 60


# -- PKCE helpers --------------------------------------------------------------

def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256 method."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# -- Public API: OAuth ---------------------------------------------------------

def generate_zoom_auth_url(csm_id: str) -> str | None:
    """Build the Zoom OAuth authorization URL with PKCE.

    Stores the code_verifier in Postgres keyed by a random state value,
    under provider "zoom". Returns the full authorization URL.

    The demo account (sarah_c) must never connect Zoom - returns None so
    the OAuth route surfaces the guard rather than minting a real URL.
    """
    from product.demo_identity import is_demo_csm_id
    if is_demo_csm_id(csm_id):
        logger.info("zoom_mcp: refusing to mint auth URL for demo account sarah_c")
        return None

    client_id = os.environ["ZOOM_CLIENT_ID"]
    redirect_uri = os.environ.get("ZOOM_REDIRECT_URI", _DEFAULT_REDIRECT_URI)
    scopes = os.environ.get("ZOOM_SCOPES", _DEFAULT_SCOPES)
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _generate_pkce_pair()
    from product.oauth_state_store import save_state
    save_state(state, "zoom", csm_id, code_verifier=code_verifier)

    params = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{_ZOOM_AUTH_URL}?{params}"


def _fetch_zoom_user_id(access_token: str) -> str:
    """Fetch the authenticated Zoom user's id via /v2/users/me.

    Best-effort and legacy: /v2/users/me requires the full-profile
    user:read:user scope, which this app intentionally does NOT request
    (it is broader than needed and would force a privacy-policy edit).
    Without that scope the call 400s, so this is EXPECTED to return "".
    Identity is instead captured from the recordings list
    (capture_zoom_host_id) at connect time, with a webhook-time scan
    fallback (resolve_csm_id_for_recording). The call is kept because it is
    cheap and harmless if a future scope ever makes it work.

    Returns the user id string, or "" on any failure. The failure is logged
    at DEBUG (not WARNING) - it is the designed path now, not an error.
    """
    try:
        resp = requests.get(
            _ZOOM_USERS_ME_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("id", "") or ""
    except Exception as exc:
        logger.debug("zoom_mcp: /v2/users/me best-effort lookup failed (no user scope): %s", exc)
        return ""


def exchange_zoom_code(code: str, state: str) -> dict:
    """Exchange an authorization code for access + refresh tokens.

    Retrieves the stored code_verifier for the given state, POSTs to
    Zoom's token endpoint, then fetches the Zoom user_id so the webhook
    can reverse-map host_id -> csm_id. Returns a flat token dict.
    """
    from product.oauth_state_store import consume_state
    entry = consume_state(state)
    if entry is None:
        raise ValueError(f"Unknown or expired OAuth state: {state!r}")
    code_verifier = entry["code_verifier"]

    client_id = os.environ["ZOOM_CLIENT_ID"]
    client_secret = os.environ["ZOOM_CLIENT_SECRET"]
    redirect_uri = os.environ.get("ZOOM_REDIRECT_URI", _DEFAULT_REDIRECT_URI)

    resp = requests.post(
        _ZOOM_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    issued_at = int(time.time())
    access_token = data["access_token"]
    # Best-effort identity via /v2/users/me. This intentionally 400s in
    # production (the app does not request user:read:user), so "" is the
    # designed outcome, not an error. The webhook reverse-map identity is
    # captured from the recordings list (capture_zoom_host_id) at connect
    # time, with a webhook-time scan fallback (resolve_csm_id_for_recording).
    zoom_user_id = _fetch_zoom_user_id(access_token)
    if not zoom_user_id:
        logger.info(json.dumps({
            "event": "zoom_connect_identity_pending",
            "detail": (
                "zoom_user_id not captured via users/me (no user scope); "
                "will capture from recordings at connect or via webhook scan"
            ),
        }))
    return {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in", 3600),
        "token_type": data.get("token_type", "Bearer"),
        "scope": data.get("scope", ""),
        "issued_at": issued_at,
        "expires_at": issued_at + data.get("expires_in", 3600),
        "zoom_user_id": zoom_user_id,
    }


def refresh_zoom_token(csm_id: str) -> dict | None:
    """Refresh the stored Zoom access token under a per-(csm_id, "zoom") lock.

    Mirrors hubspot_crm.py's double-check pattern: load the token, and if
    it is still within the refresh window after acquiring the lock, POST a
    refresh_token grant and persist the result. Preserves zoom_user_id
    across the refresh. Returns the fresh token dict, or None on failure.
    """
    from product.token_lock import token_lock

    token = load_zoom_token(csm_id)
    if not token:
        return None
    if time.time() < token.get("expires_at", 0) - _REFRESH_BUFFER_S:
        # Not actually due for refresh.
        return token

    with token_lock(csm_id, "zoom"):
        # Double-check with the SAME threshold after acquiring the lock.
        token = load_zoom_token(csm_id)
        if not token:
            return None
        if time.time() < token.get("expires_at", 0) - _REFRESH_BUFFER_S:
            return token
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            logger.warning("zoom_mcp: no refresh_token on file for %s", csm_id)
            return None
        try:
            client_id = os.environ["ZOOM_CLIENT_ID"]
            client_secret = os.environ["ZOOM_CLIENT_SECRET"]
            resp = requests.post(
                _ZOOM_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("zoom_mcp: token refresh failed for %s: %s", csm_id, exc)
            raise
        issued_at = int(time.time())
        fresh = {
            "access_token": data["access_token"],
            # Zoom rotates the refresh_token on each refresh; fall back to
            # the prior one if the response omits it.
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_in": data.get("expires_in", 3600),
            "token_type": data.get("token_type", "Bearer"),
            "scope": data.get("scope", token.get("scope", "")),
            "issued_at": issued_at,
            "expires_at": issued_at + data.get("expires_in", 3600),
            # Preserve the identity mapping across refresh.
            "zoom_user_id": token.get("zoom_user_id", ""),
        }
        save_zoom_token(csm_id, fresh)
        return fresh


# -- Public API: token storage -------------------------------------------------

def save_zoom_token(csm_id: str, token_data: dict) -> None:
    """Save the Zoom token to Postgres under provider "zoom"."""
    from product.token_store import save_token
    save_token(csm_id, "zoom", token_data)


def load_zoom_token(csm_id: str) -> dict | None:
    """Load the Zoom token from Postgres. None if not found.

    Validates csm_id the same way load_hubspot_token does, so a malformed
    identifier never reaches storage.
    """
    from product.csm_id_validation import validate_csm_id
    csm_id = validate_csm_id(csm_id)
    try:
        from product.token_store import load_token
        return load_token(csm_id, "zoom")
    except Exception:
        return None


def get_zoom_mcp_client(csm_id: str) -> dict | None:
    """Return {"token": access_token, "zoom_user_id": ...} for MCP calls.

    Loads the stored token and refreshes it if within the refresh window
    (mirrors hubspot_crm._load_and_refresh_token). Returns None when no
    usable token is on file (fail closed) so callers can branch without
    catching.
    """
    token = load_zoom_token(csm_id)
    if not token:
        return None
    if time.time() >= token.get("expires_at", 0) - _REFRESH_BUFFER_S:
        try:
            refreshed = refresh_zoom_token(csm_id)
            if refreshed:
                token = refreshed
        except Exception:
            # Refresh failed; fall through with the (possibly stale) token.
            # The MCP client's 401 retry path is the second line of defense.
            pass
    access_token = token.get("access_token")
    if not access_token:
        return None
    return {"token": access_token, "zoom_user_id": token.get("zoom_user_id", "")}


# -- Public API: webhook identity resolution -----------------------------------

def lookup_csm_id_by_zoom_user_id(zoom_host_id: str) -> str | None:
    """Resolve a Zoom host_id (from a webhook payload) to a csm_id.

    Scans every connections row with provider="zoom", parses the stored
    token_data, and returns the csm_id whose zoom_user_id matches the
    given host_id. Returns None if no row matches or the DB is
    unavailable. Never raises.
    """
    if not zoom_host_id:
        return None
    try:
        from product.database import SessionLocal, Connection
    except Exception:
        return None
    if not SessionLocal:
        return None

    db = SessionLocal()
    try:
        rows = db.query(Connection).filter_by(provider="zoom").all()
    except Exception as exc:
        logger.warning("zoom_mcp: connections lookup failed: %s", exc)
        try:
            db.close()
        except Exception:
            pass
        return None

    try:
        for row in rows:
            csm_id = getattr(row, "customer_id", None)
            if not csm_id:
                continue
            # The token is encrypted at rest; load_zoom_token decrypts it.
            token = load_zoom_token(csm_id)
            if token and token.get("zoom_user_id") == zoom_host_id:
                return csm_id
    finally:
        try:
            db.close()
        except Exception:
            pass
    return None


def capture_zoom_host_id(csm_id: str) -> str | None:
    """Capture and persist this csm's Zoom host_id from their recordings list.

    The app does NOT request user:read:user, so /v2/users/me can't supply
    the identity (see _fetch_zoom_user_id). Instead, the recordings-list
    endpoint - which we already have scope for
    (cloud_recording:read:list_user_recordings) - carries host_id per
    recording. We read the first non-empty host_id and store it as the
    token's zoom_user_id so the recording.completed webhook can reverse-map
    host_id -> csm_id on the fast path.

    Called best-effort at connect time. Returns the captured host_id, or
    None if the user has no recordings yet (the webhook scan fallback covers
    that case). Never raises.
    """
    try:
        from datetime import datetime, timedelta, timezone
        # Local import to avoid a circular import (zoom_crm imports zoom_mcp).
        from product.connectors import zoom_crm

        today = datetime.now(timezone.utc).date()
        from_date = (today - timedelta(days=30)).isoformat()
        to_date = today.isoformat()
        recordings = zoom_crm.get_recordings(csm_id, from_date, to_date)
        host_id = ""
        for rec in recordings or []:
            hid = (rec or {}).get("host_id")
            if hid:
                host_id = hid
                break
        if not host_id:
            return None
        token = load_zoom_token(csm_id)
        if not token:
            return None
        token["zoom_user_id"] = host_id
        save_zoom_token(csm_id, token)
        return host_id
    except Exception as exc:
        logger.warning(json.dumps({
            "event": "zoom_host_capture_error",
            "csm_id": csm_id,
            "detail": str(exc),
        }))
        return None


def _zoom_id_equal(a, b) -> bool:
    """Tolerant compare for Zoom recording UUIDs.

    Zoom UUIDs frequently contain "/" and "+", and the same UUID can arrive
    URL-encoded in a webhook but decoded in the recordings list (or
    double-encoded). Return True if the raw strings match, or if a single or
    double urllib.parse.unquote makes them match. Guards against None/empty.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    from urllib.parse import unquote
    try:
        a1, b1 = unquote(a), unquote(b)
        if a1 == b1:
            return True
        if unquote(a1) == unquote(b1):
            return True
    except Exception:
        return False
    return False


def resolve_csm_id_for_recording(host_id: str, recording_id: str) -> str | None:
    """Resolve a recording.completed webhook to a csm_id.

    FAST PATH: if host_id is non-empty, try lookup_csm_id_by_zoom_user_id.
    After capture-at-connect or a prior heal this is the steady state.

    SLOW PATH (only on a fast-path miss): scan every connections row with
    provider="zoom"; for each csm, list their recordings over the last 7
    days and match if the incoming recording_id matches any recording's id
    (encoding-tolerant via _zoom_id_equal) OR a recording's host_id equals
    the webhook host_id. On a match, backfill that csm's token zoom_user_id
    = host_id (canonical) so future webhooks hit the fast path (bind-once),
    then return the csm_id.

    Returns None when both paths miss. Never raises.
    """
    # FAST PATH.
    if host_id:
        hit = lookup_csm_id_by_zoom_user_id(host_id)
        if hit:
            return hit

    # SLOW PATH.
    try:
        from product.database import SessionLocal, Connection
    except Exception:
        return None
    if not SessionLocal:
        return None

    try:
        from datetime import datetime, timedelta, timezone
        from product.connectors import zoom_crm

        today = datetime.now(timezone.utc).date()
        from_date = (today - timedelta(days=7)).isoformat()
        to_date = today.isoformat()

        # Collect csm_ids, then close the session before the slow MCP calls.
        db = SessionLocal()
        try:
            rows = db.query(Connection).filter_by(provider="zoom").all()
            csm_ids = [getattr(r, "customer_id", None) for r in rows]
        finally:
            try:
                db.close()
            except Exception:
                pass

        for csm_id in csm_ids:
            if not csm_id:
                continue
            recordings = zoom_crm.get_recordings(csm_id, from_date, to_date)
            for rec in recordings or []:
                rec = rec or {}
                rec_id = rec.get("recording_id")
                rec_host = rec.get("host_id")
                matched = (
                    (recording_id and _zoom_id_equal(rec_id, recording_id))
                    or (host_id and rec_host and rec_host == host_id)
                )
                if matched:
                    # Backfill the canonical host_id so future webhooks hit
                    # the fast path (bind-once).
                    canonical = host_id or rec_host or ""
                    token = load_zoom_token(csm_id)
                    if token and canonical:
                        token["zoom_user_id"] = canonical
                        save_zoom_token(csm_id, token)
                    return csm_id
        return None
    except Exception as exc:
        logger.warning(json.dumps({
            "event": "zoom_resolve_slow_path_error",
            "host_id": host_id,
            "detail": str(exc),
        }))
        return None
