"""
product/token_store.py

Postgres-backed token storage for OAuth credentials. The connections
table keys on (csm_id, provider).

Encryption (Stage 2a, May 2026):
  save_token() serializes token_data to JSON, encrypts the JSON string
  via product.token_crypto, and persists {"ciphertext": "<fernet>"}.
  load_token() inspects the row shape:

    encrypted row :  {"ciphertext": "gAAAAA..."}     (decrypt + return dict)
    legacy row    :  any other dict shape            (return as-is, log)

  The legacy fallback is intentional during the dual-read window —
  it disappears in Stage 2f cleanup, after the backfill script has
  re-encrypted every existing row.

Filesystem fallback removed in Stage 2a. Postgres is now the only
token store. Loss of TOKEN_ENCRYPTION_KEY makes every encrypted row
permanently unrecoverable.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("cs-agent.token_store")


# Lazy import so this module loads even when the DB is not configured.
def _get_session():
    """Return a fresh SQLAlchemy session, or None if DB is unavailable."""
    try:
        from product.database import SessionLocal
        if not SessionLocal:
            return None
        return SessionLocal()
    except Exception:
        return None


def _is_encrypted_shape(raw: dict) -> bool:
    """True iff raw has exactly the key {"ciphertext"} with a Fernet-prefixed value."""
    if not isinstance(raw, dict):
        return False
    if set(raw.keys()) != {"ciphertext"}:
        return False
    value = raw.get("ciphertext")
    return isinstance(value, str) and value.startswith("gAAAAA")


def save_token(csm_id: str, provider: str, token_data: dict) -> None:
    """Encrypt and upsert token JSON into the connections table.

    Args:
        csm_id:     Clerk user ID (or legacy CSM ID like "sarah_c")
        provider:   "google" or "hubspot" or "gainsight"
        token_data: Token dict — will be JSON-serialized then encrypted.

    Raises:
        Exception: if encrypt() fails. The DB write does not happen.
                   DB-side failures are still swallowed-and-logged (existing
                   behavior) since the application has historically tolerated
                   transient Postgres errors here.
    """
    from product.token_crypto import encrypt
    try:
        ciphertext = encrypt(json.dumps(token_data))
    except Exception as exc:
        logger.error(
            "token_encrypt_failed: customer_id=%s, provider=%s, exc_type=%s",
            csm_id, provider, type(exc).__name__,
        )
        raise
    wrapped = {"ciphertext": ciphertext}

    session = _get_session()
    if session is None:
        return
    try:
        from product.database import Connection
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = (
            pg_insert(Connection)
            .values(
                id=str(uuid.uuid4()),
                customer_id=csm_id,
                provider=provider,
                token_data=wrapped,
                status="active",
                last_verified_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=["customer_id", "provider"],
                set_=dict(
                    token_data=wrapped,
                    status="active",
                    last_verified_at=datetime.now(timezone.utc),
                ),
            )
        )
        session.execute(stmt)
        session.commit()
        logger.info("Saved %s token for %s", provider, csm_id)
    except Exception as exc:
        session.rollback()
        logger.warning("save_token failed for %s/%s: %s", csm_id, provider, exc)
    finally:
        session.close()


def load_token(csm_id: str, provider: str) -> dict | None:
    """Return token dict from Postgres, or None if not found / DB unavailable.

    Encrypted rows are decrypted transparently. Legacy plaintext rows are
    returned as-is with an INFO log so the dual-read window is observable.
    Decrypt failures return None and emit structured ERROR logs that the
    founder-alert wiring (separate commit) will catch.
    """
    from product.token_crypto import decrypt, InvalidToken

    session = _get_session()
    if session is None:
        return None
    try:
        from product.database import Connection
        conn = session.query(Connection).filter_by(
            customer_id=csm_id, provider=provider
        ).first()
        if not conn or not conn.token_data:
            return None
        raw = dict(conn.token_data)
    except Exception:
        return None
    finally:
        session.close()

    if _is_encrypted_shape(raw):
        try:
            plaintext = decrypt(raw["ciphertext"])
        except InvalidToken:
            logger.error(
                "token_decrypt_key_mismatch: customer_id=%s, provider=%s",
                csm_id, provider,
            )
            return None
        try:
            return json.loads(plaintext)
        except json.JSONDecodeError:
            logger.error(
                "token_decrypt_format_invalid: customer_id=%s, provider=%s",
                csm_id, provider,
            )
            return None

    # Shape with exactly {"ciphertext"} but value not Fernet-prefixed.
    # Treat as corrupt encrypted row, not legacy plaintext.
    if isinstance(raw, dict) and set(raw.keys()) == {"ciphertext"}:
        logger.error(
            "token_decrypt_format_invalid: customer_id=%s, provider=%s",
            csm_id, provider,
        )
        return None

    # Legacy plaintext row. Expected during the dual-read window; the
    # backfill script will re-encrypt every such row before Stage 2f.
    logger.info(
        "token_decrypt_legacy_fallback: customer_id=%s, provider=%s",
        csm_id, provider,
    )
    return raw


def delete_token(csm_id: str, provider: str) -> None:
    """Remove a token from the connections table."""
    session = _get_session()
    if session is None:
        return
    try:
        from product.database import Connection
        conn = session.query(Connection).filter_by(
            customer_id=csm_id, provider=provider
        ).first()
        if conn:
            session.delete(conn)
            session.commit()
            logger.info("Deleted %s token for %s", provider, csm_id)
    except Exception as exc:
        session.rollback()
        logger.warning("delete_token failed for %s/%s: %s", csm_id, provider, exc)
    finally:
        session.close()
