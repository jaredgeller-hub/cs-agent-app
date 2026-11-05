"""
tests/test_auth_clerk.py

Verifies that product.auth_clerk.verify_clerk_token fail-closes when
CLERK_FRONTEND_API is unset, and rejects forged JWTs when a real Clerk
JWKS is configured.

Structured as standalone-runnable today; can be promoted to pytest in
Day 3's CI work without restructuring (each test_* function is a clean
unit, no shared mutable state outside test boundaries).

Usage:
    python3 tests/test_auth_clerk.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _make_forged_jwt_rs256(sub: str, email: str = "victim@example.com") -> str:
    """Forge a Clerk-shaped JWT signed with our own RSA key (Clerk has no idea)."""
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    payload = {
        "sub": sub,
        "email": email,
        "first_name": "Forged",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": "attacker_key"})


def _reload_auth_clerk():
    """Force re-import so module-level CLERK_FRONTEND_API is re-read from env."""
    import importlib
    import product.auth_clerk as ac
    importlib.reload(ac)
    return ac


def _call_verify(token: str) -> tuple[int, str]:
    """Call verify_clerk_token via a minimal FastAPI route. Returns (status, detail)."""
    from fastapi import FastAPI, Depends, Request
    from fastapi.testclient import TestClient
    ac = _reload_auth_clerk()

    app = FastAPI()

    @app.get("/_test/me")
    async def me(request: Request):
        user = await ac.verify_clerk_token(request)
        return {"ok": True, "user": user}

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/_test/me", headers={"Authorization": f"Bearer {token}"})
    try:
        detail = r.json().get("detail", "")
    except Exception:
        detail = r.text[:120]
    return r.status_code, detail


def test_missing_token_returns_401():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    ac = _reload_auth_clerk()
    app = FastAPI()

    @app.get("/_t")
    async def t(request: Request):
        await ac.verify_clerk_token(request)
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/_t")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    assert "Not authenticated" in r.text
    print("✓ missing token → 401")


def test_fail_closed_when_jwks_unset():
    """The vuln fix: with CLERK_FRONTEND_API unset, any JWT must be rejected."""
    os.environ.pop("CLERK_FRONTEND_API", None)
    forged = _make_forged_jwt_rs256(sub="user_admin", email="admin@victim.example")
    code, detail = _call_verify(forged)
    assert code == 401, f"FAIL-OPEN REGRESSION: forged JWT accepted (HTTP {code}, detail={detail!r})"
    assert "Auth not configured" in detail or "Invalid" in detail or "Could not verify" in detail
    print(f"✓ fail-closed without CLERK_FRONTEND_API → 401 ({detail!r})")


def test_forged_jwt_rejected_with_real_jwks():
    """With production JWKS configured, an attacker-signed JWT must be rejected."""
    os.environ["CLERK_FRONTEND_API"] = "clerk.cs-agent.app"
    forged = _make_forged_jwt_rs256(sub="user_admin", email="admin@victim.example")
    code, detail = _call_verify(forged)
    assert code == 401, f"FORGED JWT ACCEPTED against real JWKS (HTTP {code}, detail={detail!r})"
    print(f"✓ forged JWT rejected against real Clerk JWKS → 401 ({detail!r})")


def test_malformed_token_rejected():
    os.environ["CLERK_FRONTEND_API"] = "clerk.cs-agent.app"
    code, detail = _call_verify("not.a.real.jwt")
    assert code == 401, f"malformed token accepted (HTTP {code})"
    print(f"✓ malformed token → 401 ({detail!r})")


def main():
    failures = []
    for name, fn in [
        ("missing_token_returns_401", test_missing_token_returns_401),
        ("fail_closed_when_jwks_unset", test_fail_closed_when_jwks_unset),
        ("forged_jwt_rejected_with_real_jwks", test_forged_jwt_rejected_with_real_jwks),
        ("malformed_token_rejected", test_malformed_token_rejected),
    ]:
        try:
            fn()
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"✗ {name}: {e}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"✗ {name}: {type(e).__name__}: {e}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} of 4")
        for n, msg in failures:
            print(f"  - {n}: {msg}")
        sys.exit(1)
    print("PASSED: 4 of 4")


if __name__ == "__main__":
    main()
