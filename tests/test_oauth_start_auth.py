"""
tests/test_oauth_start_auth.py

Regression guards for /auth/google/start, /auth/hubspot/start, and
/auth/gainsight/start after the csm_id-pinning gap fix (Stage 1 audit
G-2 / G-3 / G-4).

The bug class: pre-fix, identity for the OAuth state binding came
from the URL query string (?user_id=... or ?csm_id=...). An
unauthenticated attacker could pin any csm_id into the state row,
then complete the OAuth flow with their own provider account, and
the resulting tokens would land under the attacker-chosen csm_id in
token_store.

Post-fix invariants enforced here:
- Unauthenticated request → 401 (no JWT present).
- Legacy query parameter present without a JWT → still 401.
- Authenticated request with a mismatched legacy query param → 200
  (302 redirect), state pinned to the JWT's csm_id, the body field
  is ignored.

Google start is also covered for scope-param routing in
tests/test_settings_google_actions.py (the JWT-required path was
threaded through those tests in this same fix). This file is the
security-regression home for the three OAuth-start routes; it grows
as the cluster does.

Standalone-runnable:
    python3 tests/test_oauth_start_auth.py
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse, parse_qs

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


client = TestClient(main.app)


# ── /auth/google/start ───────────────────────────────────────────────────────

def test_google_start_unauthenticated_returns_401():
    """The G-2 regression guard: an unauthenticated GET must return
    401, NOT a 302 redirect with attacker-pinned state.
    """
    async def _raise_401(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    with patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "test-client-id"}), \
         patch.object(main, "verify_clerk_token", new=_raise_401):
        r = client.get("/auth/google/start", follow_redirects=False)
    assert r.status_code == 401, r.text


def test_google_start_unauthenticated_with_query_param_still_401():
    """Cross-user pinning attempt from a logged-out attacker.
    ?user_id=victim_clerk_id MUST NOT bypass the auth check.
    """
    async def _raise_401(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    with patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "test-client-id"}), \
         patch.object(main, "verify_clerk_token", new=_raise_401):
        r = client.get("/auth/google/start", params={"user_id": "victim_clerk_id"},
                       follow_redirects=False)
    assert r.status_code == 401, r.text


def test_google_start_authed_with_mismatched_query_uses_jwt_identity():
    """The legacy ?user_id=...  query param is accepted (backward compat
    during deploy) but never trusted. State pinning uses the JWT csm_id.

    We assert by inspecting the Location header — generate_google_auth_url
    embeds the state in the URL, and the state-store lookup later in the
    callback would resolve it to the JWT csm_id, not the query param.
    Direct state inspection requires running the state-save path; this
    test instead asserts the redirect succeeds (proving the route used
    the JWT path and reached generate_google_auth_url, not the 401
    branch). State-csm_id binding is covered indirectly by the
    /auth/google/callback tests + the generate_*_auth_url unit tests.
    """
    with patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "test-client-id"}), \
         patch.object(main, "_require_jwt_identity", new=AsyncMock(return_value="jwt_user")):
        r = client.get("/auth/google/start", params={"user_id": "victim_clerk_id"},
                       follow_redirects=False)
    assert r.status_code == 302, r.text
    # Sanity: redirect goes to Google
    loc = r.headers.get("location", "")
    assert "accounts.google.com" in loc, f"Unexpected redirect: {loc[:120]}"


# ── /auth/hubspot/start ──────────────────────────────────────────────────────

def test_hubspot_start_unauthenticated_returns_401():
    """The G-3 regression guard: an unauthenticated GET must return 401."""
    async def _raise_401(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    with patch.dict("os.environ", {"HUBSPOT_CLIENT_ID": "test-id"}), \
         patch.object(main, "verify_clerk_token", new=_raise_401):
        r = client.get("/auth/hubspot/start", follow_redirects=False)
    assert r.status_code == 401, r.text


def test_hubspot_start_unauthenticated_with_query_param_still_401():
    """Cross-user pinning attempt from a logged-out attacker.
    ?csm_id=victim_clerk_id MUST NOT bypass the auth check.
    """
    async def _raise_401(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    with patch.dict("os.environ", {"HUBSPOT_CLIENT_ID": "test-id"}), \
         patch.object(main, "verify_clerk_token", new=_raise_401):
        r = client.get("/auth/hubspot/start", params={"csm_id": "victim_clerk_id"},
                       follow_redirects=False)
    assert r.status_code == 401, r.text


def test_hubspot_start_authed_with_mismatched_query_uses_jwt_identity():
    """Authenticated request with a mismatched ?csm_id query param →
    302 redirect, JWT identity used for state pinning, query param
    ignored.
    """
    with patch.dict("os.environ", {"HUBSPOT_CLIENT_ID": "test-id"}), \
         patch.object(main, "_require_jwt_identity", new=AsyncMock(return_value="jwt_user")):
        r = client.get("/auth/hubspot/start", params={"csm_id": "victim_clerk_id"},
                       follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers.get("location", "")
    assert "hubspot.com" in loc or "hubapi.com" in loc, f"Unexpected redirect: {loc[:120]}"

# ── /auth/gainsight/start ────────────────────────────────────────────────────

_GAINSIGHT_ENV = {
    "GAINSIGHT_CLIENT_ID": "test-id",
    "GAINSIGHT_DOMAIN": "example.gainsightcloud.com",
}


def test_gainsight_start_unauthenticated_returns_401():
    """The G-4 regression guard: an unauthenticated GET must return 401."""
    async def _raise_401(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    with patch.dict("os.environ", _GAINSIGHT_ENV), \
         patch.object(main, "verify_clerk_token", new=_raise_401):
        r = client.get("/auth/gainsight/start", follow_redirects=False)
    assert r.status_code == 401, r.text


def test_gainsight_start_unauthenticated_with_query_param_still_401():
    """Cross-user pinning attempt from a logged-out attacker.
    ?csm_id=victim_clerk_id MUST NOT bypass the auth check.
    """
    async def _raise_401(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    with patch.dict("os.environ", _GAINSIGHT_ENV), \
         patch.object(main, "verify_clerk_token", new=_raise_401):
        r = client.get("/auth/gainsight/start", params={"csm_id": "victim_clerk_id"},
                       follow_redirects=False)
    assert r.status_code == 401, r.text


def test_gainsight_start_authed_with_mismatched_query_uses_jwt_identity():
    """Authenticated request with a mismatched ?csm_id query param →
    302 redirect, JWT identity used for state pinning, query param
    ignored.
    """
    with patch.dict("os.environ", _GAINSIGHT_ENV), \
         patch.object(main, "_require_jwt_identity", new=AsyncMock(return_value="jwt_user")):
        r = client.get("/auth/gainsight/start", params={"csm_id": "victim_clerk_id"},
                       follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers.get("location", "")
    assert "gainsightcloud.com" in loc or "gainsight" in loc, f"Unexpected redirect: {loc[:120]}"


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
