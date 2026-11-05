"""
product/connectors/hubspot_mcp.py

HubSpot OAuth 2.1 with PKCE for the HubSpot Remote MCP Server.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import requests

from product.settings import public_url

logger = logging.getLogger("cs-agent.hubspot_mcp")

_HUBSPOT_AUTH_URL = "https://app.hubspot.com/oauth/authorize"
_HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
# Runtime default only; the redirect URIs registered with the provider
# are unchanged. Set PUBLIC_BASE_URL (or HUBSPOT_REDIRECT_URI) per deploy.
_DEFAULT_REDIRECT_URI = public_url("/auth/hubspot/callback")

# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256 method."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# ── Public API ────────────────────────────────────────────────────────────────

def generate_hubspot_auth_url(csm_id: str) -> str:
    """Build the HubSpot OAuth authorization URL with PKCE.

    Stores the code_verifier in memory keyed by a random state value.
    Returns the full authorization URL to redirect the CSM to.
    """
    client_id = os.environ["HUBSPOT_CLIENT_ID"]
    redirect_uri = os.environ.get("HUBSPOT_REDIRECT_URI", _DEFAULT_REDIRECT_URI)
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _generate_pkce_pair()
    from product.oauth_state_store import save_state
    save_state(state, "hubspot", csm_id, code_verifier=code_verifier)

    params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{_HUBSPOT_AUTH_URL}?{params}"


# TODO: remove after Day 2 — callers migrated to get_state_with_status
def get_csm_id_for_state(state: str) -> str | None:
    """Return the csm_id associated with the given OAuth state, without consuming it."""
    from product.oauth_state_store import get_state
    entry = get_state(state)
    return entry["csm_id"] if entry else None


def exchange_hubspot_code(code: str, state: str) -> dict:
    """Exchange an authorization code for access + refresh tokens.

    Retrieves the stored code_verifier for the given state, then POSTs to
    HubSpot's token endpoint. Returns the token dict.
    """
    from product.oauth_state_store import consume_state
    entry = consume_state(state)
    if entry is None:
        raise ValueError(f"Unknown or expired OAuth state: {state!r}")
    code_verifier = entry["code_verifier"]

    client_id = os.environ["HUBSPOT_CLIENT_ID"]
    client_secret = os.environ["HUBSPOT_CLIENT_SECRET"]
    redirect_uri = os.environ.get("HUBSPOT_REDIRECT_URI", _DEFAULT_REDIRECT_URI)

    resp = requests.post(
        _HUBSPOT_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    issued_at = int(time.time())
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in", 1800),
        "token_type": data.get("token_type", "Bearer"),
        "issued_at": issued_at,
        "expires_at": issued_at + data.get("expires_in", 1800),
    }


def refresh_hubspot_token(refresh_token: str) -> dict:
    """Refresh an expired HubSpot access token. Returns new token dict."""
    resp = requests.post(
        _HUBSPOT_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": os.environ["HUBSPOT_CLIENT_ID"],
            "client_secret": os.environ["HUBSPOT_CLIENT_SECRET"],
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    issued_at = int(time.time())
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_in": data.get("expires_in", 1800),
        "token_type": data.get("token_type", "Bearer"),
        "issued_at": issued_at,
        "expires_at": issued_at + data.get("expires_in", 1800),
    }


def save_hubspot_token(csm_id: str, token_data: dict) -> None:
    """Save HubSpot token to Postgres."""
    from product.token_store import save_token
    save_token(csm_id, "hubspot", token_data)


def load_hubspot_token(csm_id: str) -> dict | None:
    """Load HubSpot token from Postgres. None if not found."""
    from product.csm_id_validation import validate_csm_id
    csm_id = validate_csm_id(csm_id)
    try:
        from product.token_store import load_token
        return load_token(csm_id, "hubspot")
    except Exception:
        return None
