"""
product/connectors/gainsight_mcp.py

Gainsight OAuth 2.1 with PKCE for the Gainsight Remote MCP Server.

Mirrors product/connectors/hubspot_mcp.py. Key differences from HubSpot:
- Per-tenant domain from GAINSIGHT_DOMAIN env var (there is no single auth host).
- Token exchange / refresh bodies are JSON with camelCase keys
  (clientId, clientSecret, grantType, redirectUri, codeVerifier, refreshToken).
- The refresh grant type is "refresh_Token" with a capital T — this is what the
  Gainsight docs specify; do not "correct" it to "refresh_token".
- Refresh tokens expire in ~1 hour (much shorter than HubSpot's 30 days),
  so callers should refresh aggressively and handle refresh failures.
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

logger = logging.getLogger("cs-agent.gainsight_mcp")

# Runtime default only; the redirect URIs registered with the provider
# are unchanged. Set PUBLIC_BASE_URL (or GAINSIGHT_REDIRECT_URI) per deploy.
_DEFAULT_REDIRECT_URI = public_url("/auth/gainsight/callback")
_DEFAULT_SCOPE = "read_write"

# ── Domain / URL helpers ──────────────────────────────────────────────────────

def _domain() -> str:
    """Return the Gainsight tenant domain (e.g. 'your-tenant.us2.gainsightcloud.com').

    Raises if GAINSIGHT_DOMAIN is not set. Looked up at call time so the module
    imports cleanly in environments that don't use Gainsight.
    """
    domain = os.environ.get("GAINSIGHT_DOMAIN", "").strip()
    if not domain:
        raise EnvironmentError("GAINSIGHT_DOMAIN is not set")
    return domain.rstrip("/")


def _auth_url() -> str:
    return f"https://{_domain()}/v1/authorize"


def _token_url() -> str:
    return f"https://{_domain()}/v1/users/oauth/token"


def get_mcp_url() -> str:
    """Return the Gainsight MCP streamable-HTTP endpoint for the current tenant."""
    return f"https://{_domain()}/v1/ds-mcp/mcp"


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using the S256 method."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# ── Public API ────────────────────────────────────────────────────────────────

def generate_gainsight_auth_url(csm_id: str) -> str:
    """Build the Gainsight OAuth authorization URL with PKCE.

    Stores the code_verifier in memory keyed by a random state value.
    Returns the full authorization URL to redirect the CSM to.
    """
    client_id = os.environ["GAINSIGHT_CLIENT_ID"]
    redirect_uri = os.environ.get("GAINSIGHT_REDIRECT_URI", _DEFAULT_REDIRECT_URI)
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _generate_pkce_pair()
    from product.oauth_state_store import save_state
    save_state(state, "gainsight", csm_id, code_verifier=code_verifier)

    params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "scope": _DEFAULT_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{_auth_url()}?{params}"


# TODO: remove after Day 2 — callers migrated to get_state_with_status
def get_csm_id_for_state(state: str) -> str | None:
    """Return the csm_id associated with the given OAuth state, without consuming it."""
    from product.oauth_state_store import get_state
    entry = get_state(state)
    return entry["csm_id"] if entry else None


def exchange_gainsight_code(code: str, state: str) -> dict:
    """Exchange an authorization code for access + refresh tokens.

    Gainsight's token endpoint expects a JSON body with camelCase keys.
    """
    from product.oauth_state_store import consume_state
    entry = consume_state(state)
    if entry is None:
        raise ValueError(f"Unknown or expired OAuth state: {state!r}")
    code_verifier = entry["code_verifier"]

    client_id = os.environ["GAINSIGHT_CLIENT_ID"]
    client_secret = os.environ["GAINSIGHT_CLIENT_SECRET"]
    redirect_uri = os.environ.get("GAINSIGHT_REDIRECT_URI", _DEFAULT_REDIRECT_URI)

    resp = requests.post(
        _token_url(),
        json={
            "clientId": client_id,
            "clientSecret": client_secret,
            "grantType": "authorization_code",
            "redirectUri": redirect_uri,
            "code": code,
            "codeVerifier": code_verifier,
        },
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info("Gainsight token response keys: %s", list(data.keys()))

    # Handle both response formats
    if "data" in data and isinstance(data["data"], dict):
        # Older format: camelCase keys nested under "data"
        inner = data["data"]
        access_token = inner.get("accessToken") or inner.get("access_token", "")
        refresh_tok = inner.get("refreshToken") or inner.get("refresh_token", "")
        expires_in = int(inner.get("expiresIn") or inner.get("expires_in", 3600))
        token_type = inner.get("TokenType") or inner.get("token_type", "Bearer")
    else:
        # Newer format: snake_case keys at top level
        access_token = data.get("access_token", "")
        refresh_tok = data.get("refresh_token", "")
        expires_in = int(data.get("expires_in", 3600))
        token_type = data.get("token_type", "Bearer")

    issued_at = int(time.time())
    return {
        "access_token": access_token,
        "refresh_token": refresh_tok,
        "expires_in": expires_in,
        "token_type": token_type,
        "issued_at": issued_at,
        "expires_at": issued_at + expires_in,
    }


def refresh_gainsight_token(refresh_token: str) -> dict:
    """Refresh an expired Gainsight access token.

    Returns the new token dict. Raises on failure — callers should catch and
    prompt the user to re-authenticate, since Gainsight's refresh tokens expire
    in ~1 hour and a failed refresh means the user must go through OAuth again.

    Note the camel-case with a capital T in "refresh_Token" — this is what the
    Gainsight docs specify. Do not "correct" it.
    """
    try:
        resp = requests.post(
            _token_url(),
            json={
                "clientId": os.environ["GAINSIGHT_CLIENT_ID"],
                "clientSecret": os.environ["GAINSIGHT_CLIENT_SECRET"],
                "grantType": "refresh_Token",
                "refreshToken": refresh_token,
            },
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(
            "Gainsight token refresh failed — user may need to re-auth: %s", exc
        )
        raise

    data = resp.json()
    logger.info("Gainsight token refresh response keys: %s", list(data.keys()))

    # Handle both response formats
    if "data" in data and isinstance(data["data"], dict):
        # Older format: camelCase keys nested under "data"
        inner = data["data"]
        access_token = inner.get("accessToken") or inner.get("access_token", "")
        new_refresh_token = inner.get("refreshToken") or inner.get("refresh_token") or refresh_token
        expires_in = int(inner.get("expiresIn") or inner.get("expires_in", 3600))
        token_type = inner.get("TokenType") or inner.get("token_type", "Bearer")
    else:
        # Newer format: snake_case keys at top level
        access_token = data.get("access_token", "")
        new_refresh_token = data.get("refresh_token", refresh_token)
        expires_in = int(data.get("expires_in", 3600))
        token_type = data.get("token_type", "Bearer")

    issued_at = int(time.time())
    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "expires_in": expires_in,
        "token_type": token_type,
        "issued_at": issued_at,
        "expires_at": issued_at + expires_in,
    }


def save_gainsight_token(csm_id: str, token_data: dict) -> None:
    """Save Gainsight token to Postgres."""
    from product.token_store import save_token
    save_token(csm_id, "gainsight", token_data)


def load_gainsight_token(csm_id: str) -> dict | None:
    """Load Gainsight token from Postgres. None if not found."""
    from product.csm_id_validation import validate_csm_id
    csm_id = validate_csm_id(csm_id)
    try:
        from product.token_store import load_token
        return load_token(csm_id, "gainsight")
    except Exception:
        return None
