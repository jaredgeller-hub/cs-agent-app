"""
product/auth_clerk.py

Clerk session token verification for the React frontend.
Decodes Clerk JWTs from the Authorization: Bearer header or __session cookie.
"""

from __future__ import annotations

import logging
import os

import jwt as pyjwt
from jwt import PyJWKClient
from fastapi import HTTPException, Request

logger = logging.getLogger("cs-agent.auth_clerk")

CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")

# F15 placeholder address used when Clerk JWT carries no email claim.
# send_system_email skips recipients matching this pattern.
NOREPLY_CLERK_SUFFIX = "@noreply.clerk"


def is_noreply_email(email: str | None) -> bool:
    """True when email is the @noreply.clerk placeholder for a Clerk user."""
    return bool(email and NOREPLY_CLERK_SUFFIX in email)


def fetch_clerk_user_profile(clerk_user_id: str) -> dict | None:
    """Fetch primary email and name from the Clerk Backend API.

    Returns a dict with keys 'email', 'first_name', 'last_name', 'name'
    (concatenated) when Clerk has a primary email for this user. Returns
    None when CLERK_SECRET_KEY is unset, the API call fails, the user
    is not found, or the user has no primary email. Caller decides the
    fallback behavior.

    Never raises. All errors are logged at WARNING and converted to None.
    Logs use the opaque clerk_user_id only; never logs PII (email, name)
    at INFO level.
    """
    if not clerk_user_id:
        return None
    if not CLERK_SECRET_KEY:
        logger.warning(
            "[auth_clerk] CLERK_SECRET_KEY unset; cannot fetch profile for %s",
            clerk_user_id,
        )
        return None

    try:
        import httpx
    except Exception as exc:
        logger.warning("[auth_clerk] httpx import failed: %s", exc)
        return None

    url = f"https://api.clerk.com/v1/users/{clerk_user_id}"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning(
            "[auth_clerk] Clerk profile fetch HTTP error for %s: %s",
            clerk_user_id, exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "[auth_clerk] Clerk profile fetch unexpected error for %s: %s",
            clerk_user_id, exc,
        )
        return None

    if resp.status_code != 200:
        logger.warning(
            "[auth_clerk] Clerk profile fetch status=%s for %s",
            resp.status_code, clerk_user_id,
        )
        return None

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning(
            "[auth_clerk] Clerk profile JSON decode failed for %s: %s",
            clerk_user_id, exc,
        )
        return None

    primary_email_id = data.get("primary_email_address_id")
    email_addresses = data.get("email_addresses") or []
    primary_email: str | None = None
    for entry in email_addresses:
        if isinstance(entry, dict) and entry.get("id") == primary_email_id:
            candidate = entry.get("email_address")
            if isinstance(candidate, str) and candidate:
                primary_email = candidate
            break

    if not primary_email:
        logger.warning(
            "[auth_clerk] Clerk profile has no primary email for %s",
            clerk_user_id,
        )
        return None

    first_name = data.get("first_name") or None
    last_name = data.get("last_name") or None
    full_name = " ".join(p for p in [first_name, last_name] if p) or None

    return {
        "email": primary_email,
        "first_name": first_name,
        "last_name": last_name,
        "name": full_name,
    }


def delete_clerk_user(clerk_user_id: str) -> bool:
    """Hard-delete a user's Clerk identity via the Clerk Backend API.

    Issues DELETE https://api.clerk.com/v1/users/{clerk_user_id} with the
    same CLERK_SECRET_KEY bearer auth as fetch_clerk_user_profile. Returns
    True only on a 2xx response. Returns False when clerk_user_id is empty,
    CLERK_SECRET_KEY is unset, the API responds non-2xx, or any exception
    occurs.

    This is the second half of the delete-my-data flow: data is purged from
    Postgres + disk first, then the Clerk identity is removed so the JWT the
    user still holds can no longer re-provision a fresh trialing row via
    provision_customer_on_first_auth.

    Never raises. All errors are logged at WARNING and converted to False.
    Logs use the opaque clerk_user_id only; never logs PII.
    """
    if not clerk_user_id:
        return False
    if not CLERK_SECRET_KEY:
        logger.warning(
            "[auth_clerk] CLERK_SECRET_KEY unset; cannot delete Clerk user %s",
            clerk_user_id,
        )
        return False

    try:
        import httpx
    except Exception as exc:
        logger.warning("[auth_clerk] httpx import failed: %s", exc)
        return False

    url = f"https://api.clerk.com/v1/users/{clerk_user_id}"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.delete(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning(
            "[auth_clerk] Clerk user delete HTTP error for %s: %s",
            clerk_user_id, exc,
        )
        return False
    except Exception as exc:
        logger.warning(
            "[auth_clerk] Clerk user delete unexpected error for %s: %s",
            clerk_user_id, exc,
        )
        return False

    if 200 <= resp.status_code < 300:
        logger.info("[auth_clerk] Clerk user deleted: %s", clerk_user_id)
        return True

    logger.warning(
        "[auth_clerk] Clerk user delete status=%s for %s",
        resp.status_code, clerk_user_id,
    )
    return False


# Clerk's JWKS endpoint — derived from CLERK_FRONTEND_API env var.
# Format: https://{clerk-frontend-api-domain}/.well-known/jwks.json
_CLERK_FRONTEND_API = os.environ.get("CLERK_FRONTEND_API", "").strip().rstrip("/").removeprefix("https://").removeprefix("http://")
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient | None:
    global _jwks_client
    if _jwks_client is not None:
        return _jwks_client
    if not _CLERK_FRONTEND_API:
        logger.warning("[auth_clerk] CLERK_FRONTEND_API not set — JWT signature verification disabled")
        return None
    jwks_url = f"https://{_CLERK_FRONTEND_API}/.well-known/jwks.json"
    _jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
    logger.info("[auth_clerk] JWKS client initialized: %s", jwks_url)
    return _jwks_client


async def verify_clerk_token(request: Request) -> dict:
    """Verify a Clerk session token and return user info.

    Accepts the token from:
    - Authorization: Bearer <token>  (sent by the React frontend)
    - __session cookie               (set automatically by Clerk's JS SDK)

    Returns:
        {"user_id": str, "email": str | None, "first_name": str | None}

    Raises:
        HTTPException(401) if the token is missing or cannot be decoded.
    """
    token = None

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    if not token:
        token = request.cookies.get("__session")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        jwks = _get_jwks_client()
        if not jwks:
            logger.error("[auth_clerk] CLERK_FRONTEND_API not set — refusing to verify JWT (fail-closed)")
            raise HTTPException(status_code=401, detail="Auth not configured")
        try:
            signing_key = jwks.get_signing_key_from_jwt(token)
            decoded = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                options={
                    "verify_aud": False,  # Clerk session tokens don't always have aud
                    "verify_iss": False,  # We verify via JWKS key match instead
                },
            )
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Session expired")
        except pyjwt.InvalidTokenError as jwt_err:
            logger.warning("[auth_clerk] JWT verification failed: %s", jwt_err)
            raise HTTPException(status_code=401, detail="Invalid session token")
        except HTTPException:
            raise
        except Exception as jwks_err:
            logger.error("[auth_clerk] JWKS fetch/key resolution failed: %s", jwks_err)
            raise HTTPException(status_code=401, detail="Could not verify session")
        # Clerk may put email in top-level claim, under metadata, or not at all
        email = (
            decoded.get("email")
            or decoded.get("primary_email")
            or (decoded.get("unsafe_metadata") or {}).get("email")
            or (decoded.get("public_metadata") or {}).get("email")
        )
        # Clerk JWTs may store name in top-level claims, metadata, or not at all
        first_name = (
            decoded.get("first_name")
            or decoded.get("given_name")
            or (decoded.get("user_metadata") or {}).get("first_name")
            or (decoded.get("unsafe_metadata") or {}).get("first_name")
            or (decoded.get("public_metadata") or {}).get("first_name")
        )
        last_name = (
            decoded.get("last_name")
            or decoded.get("family_name")
            or (decoded.get("user_metadata") or {}).get("last_name")
            or (decoded.get("unsafe_metadata") or {}).get("last_name")
        )
        # Build display name: "First" or "First Last", never a raw user_id
        display_name = f"{first_name} {last_name}".strip() if first_name else None
        # Fall back to email prefix if no name at all
        if not display_name and email and "@" in email and "@noreply.clerk" not in email:
            display_name = email.split("@")[0].replace(".", " ").title()

        user_info = {
            "user_id": decoded.get("sub"),
            "email": email,
            "first_name": first_name or display_name,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[auth_clerk] Token decode failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid session token")

    # Ensure a customer record exists in Postgres (best-effort, never blocks auth).
    # provision_customer_on_first_auth: INSERT trialing+14d on new row, UPDATE
    # only last_login_at on existing row. subscription_status never overwritten.
    try:
        from product.subscription import provision_customer_on_first_auth
        provision_customer_on_first_auth(
            clerk_user_id=user_info["user_id"] or "",
            email=user_info["email"],
            name=user_info["first_name"],
        )

        # F15 self-heal. If the JWT lacked email or name, fetch them from
        # the Clerk Backend API. Catches existing rows that were provisioned
        # before fetch_clerk_user_profile was wired in. Falls through to the
        # backfill script if Clerk also has no primary email.
        target_email = user_info["email"]
        target_name = user_info["first_name"]
        if (not target_email) or is_noreply_email(target_email) or not target_name:
            profile = fetch_clerk_user_profile(user_info["user_id"] or "")
            if profile:
                if profile.get("email") and ((not target_email) or is_noreply_email(target_email)):
                    target_email = profile["email"]
                if profile.get("name") and not target_name:
                    target_name = profile["name"]

        if target_email and not is_noreply_email(target_email):
            try:
                from product.database import SessionLocal, Customer
                if SessionLocal:
                    session = SessionLocal()
                    try:
                        cust = session.query(Customer).filter_by(
                            clerk_user_id=user_info["user_id"]
                        ).first()
                        if cust:
                            changed = False
                            if cust.email and is_noreply_email(cust.email):
                                cust.email = target_email
                                changed = True
                            if target_name and not cust.name:
                                cust.name = target_name
                                changed = True
                            if changed:
                                session.commit()
                                logger.info(
                                    "[auth_clerk] Self-heal updated customer for %s",
                                    user_info["user_id"],
                                )
                    finally:
                        session.close()
            except Exception as _eu:
                logger.warning("[auth_clerk] Self-heal failed (non-fatal): %s", _eu)
    except Exception as exc:
        logger.warning("[auth_clerk] DB upsert failed (non-fatal): %s", exc)

    return user_info
