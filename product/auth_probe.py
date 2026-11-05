"""
product/auth_probe.py

Probe-on-demand token-validity check for /app/connection-status.

For each provider (Google, HubSpot, Gainsight), determine whether the
stored token will produce a working API call right now. Distinguishes:

- "ok"      : token present and refresh succeeded (or wasn't needed)
- "expired" : token present, refresh failed (refresh_token revoked)
- "missing" : no token in storage
- "error"   : transient probe failure (network, 5xx)

Connection.status / Connection.last_verified_at columns are
intentionally NOT touched by this module. Both are dead infrastructure
today; making them meaningful is a separate card.

The probe never raises out to the caller. Every code path returns one
of the four state strings.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("cs-agent.auth_probe")


# Module-level cache: csm_id -> (status_dict, timestamp)
# Process-local. uvicorn runs single-worker per CLAUDE.md, so a Python
# dict + Lock is sufficient.
_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[dict, float]] = {}
_cache_lock = threading.Lock()

_REFRESH_RETRY_GAP_S = 1.0


def get_cached_status(csm_id: str) -> Optional[dict]:
    """Return cached status if fresh, else None."""
    with _cache_lock:
        entry = _cache.get(csm_id)
    if entry is None:
        return None
    status, ts = entry
    if time.time() - ts > _CACHE_TTL_S:
        return None
    return status


def set_cached_status(csm_id: str, status: dict) -> None:
    with _cache_lock:
        _cache[csm_id] = (status, time.time())


def invalidate_cache(csm_id: str) -> None:
    """Drop the cache entry for a user. Called from OAuth callbacks
    after a successful reconnect so the next /app/connection-status
    re-probes immediately."""
    with _cache_lock:
        _cache.pop(csm_id, None)


def _emit_probe_log(csm_id: str, provider: str, result: str, duration_ms: int) -> None:
    logger.info(
        "event=connection_probe csm_id=%s provider=%s result=%s duration_ms=%d",
        csm_id, provider, result, duration_ms,
    )


# ── Google ───────────────────────────────────────────────────────────────────


def _probe_google(csm_id: str) -> str:
    """Google probe. Loads creds, refreshes if expired, returns state.

    Best-of-2 retry on the refresh path: a transient Google rate limit
    or 5xx during refresh should not flag the user as "expired" the
    first time. Only flag after both attempts fail.
    """
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError

    try:
        from product.auth_google import load_google_token
    except Exception:
        return "error"

    try:
        token_data = load_google_token(csm_id)
    except Exception:
        return "error"

    if not token_data:
        return "missing"

    try:
        from google.oauth2.credentials import Credentials
        from product.auth_utils import load_google_credentials
        try:
            creds = load_google_credentials(csm_id)
        except FileNotFoundError:
            return "missing"
        except RefreshError:
            return "expired"
        except Exception:
            return "error"
    except Exception:
        return "error"

    if not creds:
        return "missing"

    if not creds.expired:
        return "ok"

    if not creds.refresh_token:
        return "expired"

    last_error = None
    for attempt in range(2):
        try:
            creds.refresh(Request())
            return "ok"
        except RefreshError as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(_REFRESH_RETRY_GAP_S)
                continue
            return "expired"
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(_REFRESH_RETRY_GAP_S)
                continue
            return "error"

    if last_error and isinstance(last_error, RefreshError):
        return "expired"
    return "error"


# ── HubSpot ──────────────────────────────────────────────────────────────────


def _probe_hubspot(csm_id: str) -> str:
    """HubSpot probe. Token refresh window mirrors hubspot_crm.py:188
    (refresh if expires_at within 60s)."""
    try:
        from product.connectors.hubspot_mcp import (
            load_hubspot_token,
            refresh_hubspot_token,
        )
    except Exception:
        return "error"

    try:
        token = load_hubspot_token(csm_id)
    except Exception:
        return "error"

    if not token:
        return "missing"

    expires_at = token.get("expires_at", 0)
    if time.time() < expires_at - 60:
        return "ok"

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        return "expired"

    last_error_kind = None
    for attempt in range(2):
        try:
            refresh_hubspot_token(refresh_token)
            return "ok"
        except Exception as exc:
            msg = str(exc).lower()
            if "invalid" in msg or "revoked" in msg or "unauthorized" in msg or "400" in msg:
                last_error_kind = "expired"
            else:
                last_error_kind = "error"
            if attempt == 0:
                time.sleep(_REFRESH_RETRY_GAP_S)
                continue

    return last_error_kind or "error"


# ── Gainsight ────────────────────────────────────────────────────────────────


def _probe_gainsight(csm_id: str) -> str:
    """Gainsight probe. Tokens expire hourly so probe usually triggers
    a real refresh. Buffer mirrors gainsight.py:190 (300s)."""
    try:
        from product.connectors.gainsight_mcp import (
            load_gainsight_token,
            refresh_gainsight_token,
        )
    except Exception:
        return "error"

    try:
        token = load_gainsight_token(csm_id)
    except Exception:
        return "error"

    if not token:
        return "missing"

    expires_at = token.get("expires_at", 0)
    if time.time() < expires_at - 300:
        return "ok"

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        return "expired"

    last_error_kind = None
    for attempt in range(2):
        try:
            refresh_gainsight_token(refresh_token)
            return "ok"
        except Exception as exc:
            msg = str(exc).lower()
            if "invalid" in msg or "revoked" in msg or "unauthorized" in msg or "400" in msg:
                last_error_kind = "expired"
            else:
                last_error_kind = "error"
            if attempt == 0:
                time.sleep(_REFRESH_RETRY_GAP_S)
                continue

    return last_error_kind or "error"


# ── Slack ────────────────────────────────────────────────────────────────────


def _probe_slack(csm_id: str) -> str:
    """Slack probe. Slack user tokens (xoxp-) are long-lived with no
    refresh path, so this is presence + liveness only:

    - "missing" : no token in storage.
    - "ok"      : token present and auth.test confirms it is still valid.
    - "expired" : token present but auth.test reports an auth error
                  (e.g. token_revoked, invalid_auth, account_inactive).
    - "error"   : transient probe failure (network, 5xx, unexpected error).
    """
    try:
        from product.connectors.slack_mcp import load_slack_token
    except Exception:
        return "error"

    try:
        token = load_slack_token(csm_id)
    except Exception:
        return "error"

    if not token:
        return "missing"

    access_token = token.get("access_token")
    if not access_token:
        return "missing"

    import requests
    _AUTH_ERRORS = {
        "token_revoked", "invalid_auth", "account_inactive",
        "token_expired", "not_authed", "no_permission",
    }
    last_kind = None
    for attempt in range(2):
        try:
            resp = requests.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                return "ok"
            if data.get("error") in _AUTH_ERRORS:
                return "expired"
            last_kind = "error"
        except Exception:
            last_kind = "error"
        if attempt == 0:
            time.sleep(_REFRESH_RETRY_GAP_S)
            continue

    return last_kind or "error"


# ── Zoom ─────────────────────────────────────────────────────────────────────


def _probe_zoom(csm_id: str) -> str:
    """Zoom probe. Zoom issues refresh tokens, so this mirrors the HubSpot
    probe: refresh if within the buffer window, then confirm liveness with
    a GET /v2/users/me.

    - "missing" : no token in storage.
    - "ok"      : token present and /v2/users/me returns 200.
    - "expired" : refresh failed, or /v2/users/me returns 401.
    - "error"   : transient probe failure (network, 5xx, unexpected error).
    """
    try:
        from product.connectors.zoom_mcp import load_zoom_token, refresh_zoom_token
    except Exception:
        return "error"

    try:
        token = load_zoom_token(csm_id)
    except Exception:
        return "error"

    if not token:
        return "missing"

    expires_at = token.get("expires_at", 0)
    if time.time() >= expires_at - 300:
        try:
            refreshed = refresh_zoom_token(csm_id)
        except Exception:
            return "expired"
        if not refreshed:
            return "expired"
        token = refreshed

    access_token = token.get("access_token")
    if not access_token:
        return "missing"

    import requests
    last_kind = None
    for attempt in range(2):
        try:
            resp = requests.get(
                "https://api.zoom.us/v2/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            if resp.status_code == 200:
                return "ok"
            if resp.status_code == 401:
                return "expired"
            last_kind = "error"
        except Exception:
            last_kind = "error"
        if attempt == 0:
            time.sleep(_REFRESH_RETRY_GAP_S)
            continue

    return last_kind or "error"


# ── Public entry point ───────────────────────────────────────────────────────


def probe_all(csm_id: str) -> dict:
    """Probe all four providers and return a status dict.

    Result shape:
        {"gmail": "ok", "calendar": "ok", "hubspot": "missing",
         "gainsight": "missing", "slack": "missing"}

    Gmail and Calendar share Google credentials so they always have
    the same state. Reported separately for UI clarity.
    """
    cached = get_cached_status(csm_id)
    if cached is not None:
        return cached

    start = time.perf_counter()
    google_state = _probe_google(csm_id)
    _emit_probe_log(csm_id, "google", google_state, int((time.perf_counter() - start) * 1000))

    start = time.perf_counter()
    hubspot_state = _probe_hubspot(csm_id)
    _emit_probe_log(csm_id, "hubspot", hubspot_state, int((time.perf_counter() - start) * 1000))

    start = time.perf_counter()
    gainsight_state = _probe_gainsight(csm_id)
    _emit_probe_log(csm_id, "gainsight", gainsight_state, int((time.perf_counter() - start) * 1000))

    start = time.perf_counter()
    slack_state = _probe_slack(csm_id)
    _emit_probe_log(csm_id, "slack", slack_state, int((time.perf_counter() - start) * 1000))

    start = time.perf_counter()
    zoom_state = _probe_zoom(csm_id)
    _emit_probe_log(csm_id, "zoom", zoom_state, int((time.perf_counter() - start) * 1000))

    status = {
        "gmail": google_state,
        "calendar": google_state,
        "hubspot": hubspot_state,
        "gainsight": gainsight_state,
        "slack": slack_state,
        "zoom": zoom_state,
    }
    set_cached_status(csm_id, status)
    return status
