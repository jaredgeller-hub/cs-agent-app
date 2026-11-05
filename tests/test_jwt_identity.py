"""
tests/test_jwt_identity.py

Verifies main._require_jwt_identity — the JWT-only entry helper used by
the OAuth-start routes after the body-controlled-identity gap fix
(G-2 / G-3 / G-4).

Invariants enforced:
- A valid Clerk JWT yields the raw user_id claim, passed through
  _validate_csm_id (defense against path traversal at the boundary).
- No JWT → 401.
- Falsy user_id in JWT (missing key, empty string, None) → 401.
- user_id present but failing csm_id validation → 400.

Stage 1 audit reference: see report at /app/onboarding/complete + the
three /auth/*/start routes. Bug class: body-controlled identity.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from product.settings import DEMO_EMAIL  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

class _StubRequest:
    """Minimal Request stand-in. _require_jwt_identity only forwards it
    to verify_clerk_token, which we monkeypatch."""
    pass


def _call(stub_request):
    """Run the async helper synchronously for tests.

    Uses asyncio.run() rather than get_event_loop(), which raises
    RuntimeError in Python 3.11+ when no loop exists in the current
    thread (CLAUDE.md Critical Rule #8). Each call gets a fresh loop.
    """
    import main
    return asyncio.run(main._require_jwt_identity(stub_request))


# ── Happy path ───────────────────────────────────────────────────────────────

def test_returns_validated_user_id_from_jwt(monkeypatch):
    """Valid JWT with a well-formed user_id returns it (passed through validator)."""
    async def fake_verify(request):
        return {"user_id": "user_2abcDEF123ghiJKLmnop456qRsTU", "email": "x@y.example", "first_name": "Test"}
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    result = _call(_StubRequest())
    assert result == "user_2abcDEF123ghiJKLmnop456qRsTU"


def test_validated_user_id_passes_through_validator(monkeypatch):
    """A user_id that survives _validate_csm_id is returned unchanged."""
    async def fake_verify(request):
        return {"user_id": "sarah_c", "email": DEMO_EMAIL}
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    result = _call(_StubRequest())
    assert result == "sarah_c"


# ── Falsy-present cases (Finding 1 refinement) ───────────────────────────────

def test_raises_401_when_user_id_key_missing(monkeypatch):
    """JWT dict with no user_id key at all → 401 'JWT missing identity claim'."""
    async def fake_verify(request):
        return {"email": "x@y.example", "first_name": "NoID"}
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    with pytest.raises(HTTPException) as exc:
        _call(_StubRequest())
    assert exc.value.status_code == 401
    assert "missing identity claim" in exc.value.detail


def test_raises_401_when_user_id_empty_string(monkeypatch):
    """JWT dict with user_id='' → 401 (falsy)."""
    async def fake_verify(request):
        return {"user_id": "", "email": "x@y.example"}
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    with pytest.raises(HTTPException) as exc:
        _call(_StubRequest())
    assert exc.value.status_code == 401
    assert "missing identity claim" in exc.value.detail


def test_raises_401_when_user_id_none(monkeypatch):
    """JWT dict with user_id=None → 401 (falsy)."""
    async def fake_verify(request):
        return {"user_id": None, "email": "x@y.example"}
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    with pytest.raises(HTTPException) as exc:
        _call(_StubRequest())
    assert exc.value.status_code == 401
    assert "missing identity claim" in exc.value.detail


# ── verify_clerk_token failure propagation ───────────────────────────────────

def test_propagates_401_when_verify_clerk_token_raises_401(monkeypatch):
    """No JWT present → verify_clerk_token raises 401 → helper propagates."""
    async def fake_verify(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    with pytest.raises(HTTPException) as exc:
        _call(_StubRequest())
    assert exc.value.status_code == 401
    assert exc.value.detail == "Not authenticated"


def test_propagates_session_expired(monkeypatch):
    """Expired JWT → verify_clerk_token raises 401 'Session expired' → propagates."""
    async def fake_verify(request):
        raise HTTPException(status_code=401, detail="Session expired")
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    with pytest.raises(HTTPException) as exc:
        _call(_StubRequest())
    assert exc.value.status_code == 401
    assert exc.value.detail == "Session expired"


# ── _validate_csm_id boundary check ──────────────────────────────────────────

def test_raises_400_when_user_id_fails_csm_id_validation(monkeypatch):
    """JWT user_id with path-traversal sequence → 400 from _validate_csm_id."""
    async def fake_verify(request):
        return {"user_id": "user_../etc/passwd", "email": "x@y.example"}
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    with pytest.raises(HTTPException) as exc:
        _call(_StubRequest())
    # _validate_csm_id raises 400 "Invalid csm_id"
    assert exc.value.status_code == 400


def test_raises_400_when_user_id_contains_null_byte(monkeypatch):
    """JWT user_id with null byte → 400 from _validate_csm_id (defense in depth)."""
    async def fake_verify(request):
        return {"user_id": "user_abc\x00xyz", "email": "x@y.example"}
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    with pytest.raises(HTTPException) as exc:
        _call(_StubRequest())
    assert exc.value.status_code == 400


def test_raises_400_when_user_id_too_long(monkeypatch):
    """JWT user_id > 64 chars → 400 from _validate_csm_id."""
    async def fake_verify(request):
        return {"user_id": "user_" + "a" * 70, "email": "x@y.example"}
    import main
    monkeypatch.setattr(main, "verify_clerk_token", fake_verify)
    with pytest.raises(HTTPException) as exc:
        _call(_StubRequest())
    assert exc.value.status_code == 400
