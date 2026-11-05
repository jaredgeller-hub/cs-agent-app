"""
product/oauth_state_store.py

Postgres-backed OAuth state storage with TTL.
Replaces the in-memory dicts in auth_google.py, hubspot_mcp.py, gainsight_mcp.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("cs-agent.oauth_state_store")

from product.database import as_utc

_TTL_MINUTES = 30  # Bumped from 10 on 2026-04-18: real users routinely take
                   # >10min on Google's consent flow (unverified-app warning,
                   # Advanced clickthrough, scope review). Uniform across all
                   # three providers — no per-provider branching.


def save_state(
    state: str,
    provider: str,
    csm_id: str,
    code_verifier: str | None = None,
) -> bool:
    """Save an OAuth state to Postgres. Returns True on success, False on failure."""
    try:
        from product.database import SessionLocal, OAuthState
        if not SessionLocal:
            logger.warning("oauth_state_store: DB not configured")
            return False
        session = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            # Piggyback cleanup: delete expired states (keep table small)
            session.query(OAuthState).filter(OAuthState.expires_at < now).delete()
            session.add(OAuthState(
                state=state,
                provider=provider,
                csm_id=csm_id,
                code_verifier=code_verifier,
                created_at=now,
                expires_at=now + timedelta(minutes=_TTL_MINUTES),
            ))
            session.commit()
            return True
        except Exception as exc:
            session.rollback()
            logger.warning("oauth_state_store: save failed: %s", exc)
            return False
        finally:
            session.close()
    except Exception as exc:
        logger.warning("oauth_state_store: import/init failed: %s", exc)
        return False


def get_state(state: str) -> dict | None:
    """Look up an OAuth state without consuming it. Returns dict with csm_id and code_verifier, or None.

    Legacy API — returns None for both "not found" and "expired". For error
    differentiation in callback handlers, use get_state_with_status() instead.
    """
    entry, status = get_state_with_status(state)
    return entry if status == "ok" else None


def get_state_with_status(state: str) -> tuple[dict | None, str]:
    """Non-consuming lookup that differentiates 'not_found' vs. 'expired'.

    Returns (entry, status) where:
      - status == "ok":        entry is {csm_id, code_verifier, provider}
      - status == "expired":   entry is None, row existed but is past expires_at
      - status == "not_found": entry is None, no row matches this state
    """
    try:
        from product.database import SessionLocal, OAuthState
        if not SessionLocal:
            return None, "not_found"
        session = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            row = session.query(OAuthState).filter_by(state=state).first()
            if row is None:
                return None, "not_found"
            if as_utc(row.expires_at) < now:
                return None, "expired"
            return {
                "csm_id": row.csm_id,
                "code_verifier": row.code_verifier,
                "provider": row.provider,
            }, "ok"
        finally:
            session.close()
    except Exception:
        return None, "not_found"


def consume_state(state: str) -> dict | None:
    """Look up AND delete an OAuth state (one-time use). Returns dict or None."""
    try:
        from product.database import SessionLocal, OAuthState
        if not SessionLocal:
            return None
        session = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            row = session.query(OAuthState).filter_by(state=state).first()
            if row is None:
                logger.info("OAuth state consumed: status=not_found")
                return None
            provider = row.provider
            elapsed = (now - as_utc(row.created_at)).total_seconds()
            if as_utc(row.expires_at) < now:
                session.delete(row)
                session.commit()
                logger.info(
                    "OAuth state consumed: provider=%s, elapsed_seconds=%.1f, status=expired",
                    provider, elapsed,
                )
                return None
            result = {
                "csm_id": row.csm_id,
                "code_verifier": row.code_verifier,
                "provider": row.provider,
            }
            session.delete(row)
            session.commit()
            logger.info(
                "OAuth state consumed: provider=%s, elapsed_seconds=%.1f, status=ok",
                provider, elapsed,
            )
            return result
        except Exception as exc:
            session.rollback()
            logger.warning("oauth_state_store: consume failed: %s", exc)
            return None
        finally:
            session.close()
    except Exception:
        return None
