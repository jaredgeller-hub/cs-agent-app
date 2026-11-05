"""
product/connectors/slack_mcp.py

Slack OAuth 2.1 with PKCE for the per-user Slack user token (xoxp-).

Mirrors hubspot_mcp.py, with three Slack-specific differences:
  1. The authorize endpoint uses user_scope= (not scope=) so the grant
     issues a user token instead of a bot token.
  2. The token response carries the user token at
     response["authed_user"]["access_token"], not the top-level
     access_token.
  3. There is NO refresh path. Slack user tokens (xoxp-) are long-lived;
     unless token rotation is explicitly enabled on the app they never
     expire. If a token is missing or invalid the callers fail closed.
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

logger = logging.getLogger("cs-agent.slack_mcp")

_SLACK_AUTH_URL = "https://slack.com/oauth/v2/authorize"
_SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
# Runtime default only; the redirect URIs registered with the provider
# are unchanged. Set PUBLIC_BASE_URL (or SLACK_REDIRECT_URI) per deploy.
_DEFAULT_REDIRECT_URI = public_url("/auth/slack/callback")

# User-token scopes for the two capabilities in this build:
#   search:read        - inbound signals via search.messages
#   chat:write         - outbound send via chat.postMessage
#   users:read         - resolve display names / auth.test identity
#   channels:read      - resolve channel names to IDs for outbound
#   users:read.email   - resolve a teammate email to a user for DMs
_DEFAULT_USER_SCOPES = "search:read,chat:write,users:read,channels:read,users:read.email"


# -- PKCE helpers --------------------------------------------------------------

def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256 method."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# -- Public API ----------------------------------------------------------------

def generate_slack_auth_url(csm_id: str) -> str:
    """Build the Slack OAuth authorization URL with PKCE.

    Stores the code_verifier in Postgres keyed by a random state value,
    under provider "slack". Returns the full authorization URL to redirect
    the CSM to.

    The demo account (sarah_c) must never connect Slack; raise so the
    OAuth route surfaces the guard rather than minting a real auth URL.
    """
    from product.demo_identity import is_demo_csm_id
    if is_demo_csm_id(csm_id):
        raise ValueError("Slack connection is not available for the demo account")

    client_id = os.environ["SLACK_CLIENT_ID"]
    redirect_uri = os.environ.get("SLACK_REDIRECT_URI", _DEFAULT_REDIRECT_URI)
    user_scopes = os.environ.get("SLACK_USER_SCOPES", _DEFAULT_USER_SCOPES)
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _generate_pkce_pair()
    from product.oauth_state_store import save_state
    save_state(state, "slack", csm_id, code_verifier=code_verifier)

    # Slack uses user_scope= (not scope=) for user-token grants.
    params = urlencode({
        "client_id": client_id,
        "user_scope": user_scopes,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{_SLACK_AUTH_URL}?{params}"


def exchange_slack_code(code: str, state: str) -> dict:
    """Exchange an authorization code for a Slack user token (xoxp-).

    Retrieves the stored code_verifier for the given state, POSTs to
    Slack's oauth.v2.access endpoint, and normalizes the response into a
    flat token dict before returning it. The user token lives at
    response["authed_user"]["access_token"].

    Slack returns HTTP 200 with {"ok": false, "error": "..."} on failure,
    so ok is checked explicitly rather than relying on raise_for_status.
    """
    from product.oauth_state_store import consume_state
    entry = consume_state(state)
    if entry is None:
        raise ValueError(f"Unknown or expired OAuth state: {state!r}")
    code_verifier = entry["code_verifier"]

    client_id = os.environ["SLACK_CLIENT_ID"]
    client_secret = os.environ["SLACK_CLIENT_SECRET"]
    redirect_uri = os.environ.get("SLACK_REDIRECT_URI", _DEFAULT_REDIRECT_URI)

    resp = requests.post(
        _SLACK_TOKEN_URL,
        data={
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
    if not data.get("ok"):
        raise ValueError(f"Slack oauth.v2.access failed: {data.get('error', 'unknown_error')}")

    authed_user = data.get("authed_user") or {}
    user_token = authed_user.get("access_token")
    if not user_token:
        raise ValueError("Slack response missing authed_user.access_token (user token)")

    team = data.get("team") or {}
    return {
        # Flat shape so token_store / load_slack_token callers read a plain dict.
        "access_token": user_token,
        "token_type": authed_user.get("token_type", "user"),
        "scope": authed_user.get("scope", ""),
        "slack_user_id": authed_user.get("id", ""),
        "team_id": team.get("id", ""),
        "team_name": team.get("name", ""),
        "issued_at": int(time.time()),
        # No expires_at: xoxp- user tokens are long-lived. Absence of this
        # key is the signal that there is no refresh path for Slack.
    }


def save_slack_token(csm_id: str, token_data: dict) -> None:
    """Save the Slack token to Postgres under provider "slack"."""
    from product.token_store import save_token
    save_token(csm_id, "slack", token_data)


def load_slack_token(csm_id: str) -> dict | None:
    """Load the Slack token from Postgres. None if not found.

    Validates csm_id the same way load_hubspot_token does, so a malformed
    identifier never reaches storage.
    """
    from product.csm_id_validation import validate_csm_id
    csm_id = validate_csm_id(csm_id)
    try:
        from product.token_store import load_token
        return load_token(csm_id, "slack")
    except Exception:
        return None


def get_slack_client(csm_id: str) -> dict | None:
    """Return a thin client dict {"token": xoxp_token} for Slack Web API calls.

    There is no SDK dependency; callers use the token directly with
    requests against https://slack.com/api/*. Returns None when no token
    is on file (fail closed) so callers can branch without catching.
    """
    token_data = load_slack_token(csm_id)
    if not token_data:
        return None
    access_token = token_data.get("access_token")
    if not access_token:
        return None
    return {"token": access_token, "slack_user_id": token_data.get("slack_user_id", "")}
