"""
product/auth_google.py

Google OAuth 2.0 web flow for Gmail + Calendar access.
Mirrors the HubSpot MCP pattern in product/connectors/hubspot_mcp.py.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from product.settings import public_url

logger = logging.getLogger("cs-agent.auth_google")

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Runtime default only; the redirect URIs registered with the provider
# are unchanged. Set PUBLIC_BASE_URL (or GOOGLE_REDIRECT_URI) per deploy.
_DEFAULT_REDIRECT_URI = public_url("/auth/google/callback")

SCOPES_READONLY = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.meet.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# Progressive-unlock write scope set for Google actions.
# Adds gmail.send (send email on behalf) on top of the readonly base.
# On Google's consent screen with CSM-facing string: "Send email on
# your behalf." No gmail.modify, no broader calendar scope.
# calendar.events removed April 23 — feature not built; will re-add via
# supplementary verification when calendar write ships.
SCOPES_WRITE = SCOPES_READONLY + [
    "https://www.googleapis.com/auth/gmail.send",
]

# Demo data seeding scope set. Adds gmail.modify (required for
# users.messages.import — gmail.send authorizes only messages.send),
# calendar.events (insert/delete events on primary), and drive.file
# (create/manage Drive files the app created) on top of SCOPES_WRITE.
# Used only by scripts/seed_csm_data.py to stage realistic demo data
# for recording sessions. Never requested for end users — no consent
# prompt a real CSM would otherwise see.
SCOPES_SEED = SCOPES_WRITE + [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.file",
]

# Default scopes for new users (readonly)
SCOPES = SCOPES_READONLY

# ── Public API ────────────────────────────────────────────────────────────────

def generate_google_auth_url(user_id: str, scopes: list[str] | None = None) -> str:
    """Generate Google OAuth authorization URL and store state for callback."""
    _scopes = scopes or SCOPES
    state = secrets.token_urlsafe(32)
    from product.oauth_state_store import save_state
    save_state(state, "google", user_id)

    params = urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": os.environ.get("GOOGLE_REDIRECT_URI", _DEFAULT_REDIRECT_URI),
        "response_type": "code",
        "scope": " ".join(_scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return f"{_GOOGLE_AUTH_URL}?{params}"


# TODO: remove after Day 2 — callers migrated to get_state_with_status
def get_user_id_for_state(state: str) -> str | None:
    """Return the user_id for a given state without consuming it."""
    from product.oauth_state_store import get_state
    entry = get_state(state)
    return entry["csm_id"] if entry else None


def exchange_google_code(code: str, state: str) -> dict:
    """Exchange authorization code for tokens. Consumes the state entry."""
    from product.oauth_state_store import consume_state
    entry = consume_state(state)
    if entry is None:
        raise ValueError(f"Unknown or expired OAuth state: {state!r}")

    resp = requests.post(
        _GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": os.environ.get("GOOGLE_REDIRECT_URI", _DEFAULT_REDIRECT_URI),
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    resp.raise_for_status()
    token_data = resp.json()
    token_data["obtained_at"] = datetime.now(timezone.utc).isoformat()
    token_data["user_id"] = entry["csm_id"]
    return token_data


def save_google_token(user_id: str, token_data: dict) -> None:
    """Save Google token to Postgres.

    The stored dict format matches what google.oauth2.credentials.Credentials
    expects via from_authorized_user_info().

    Refresh-token preservation: Google's re-auth responses sometimes omit
    refresh_token even when prompt=consent is set. Overwriting the stored
    value with None would destroy the user's offline-access capability.
    If the incoming response has no refresh_token, reuse the prior value
    from storage. Log a WARNING if there's no prior value either (first
    save without a refresh_token — rare but surfaces misconfiguration).
    """
    from product.csm_id_validation import validate_csm_id
    user_id = validate_csm_id(user_id)

    # Preserve prior refresh_token if the incoming response omitted it
    incoming_refresh = token_data.get("refresh_token")
    if not incoming_refresh:
        prior = load_google_token(user_id)
        if prior and prior.get("refresh_token"):
            token_data = {**token_data, "refresh_token": prior["refresh_token"]}
        else:
            logger.warning("refresh_token missing on first save for user=%s", user_id)

    # Use actual granted scopes from Google's token response, not hardcoded defaults
    _granted = token_data.get("scope", "")
    _granted_scopes = _granted.split() if _granted else SCOPES
    credentials_data = {
        "token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "token_uri": _GOOGLE_TOKEN_URL,
        "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
        "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
        "scopes": _granted_scopes,
    }

    from product.token_store import save_token
    save_token(user_id, "google", credentials_data)


def load_google_token(user_id: str) -> dict | None:
    """Load Google token from Postgres. None if not found."""
    from product.csm_id_validation import validate_csm_id
    user_id = validate_csm_id(user_id)
    try:
        from product.token_store import load_token
        return load_token(user_id, "google")
    except Exception:
        return None
