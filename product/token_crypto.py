"""
product/token_crypto.py

Application-layer symmetric encryption for OAuth tokens.

Fernet (AES-128-CBC + HMAC-SHA256) is the standard primitive for
authenticated symmetric encryption in the Python cryptography library.
Every Fernet ciphertext begins with "gAAAAA" (the v1 wire-format
identifier) — that prefix is the discriminator used by token_store
to distinguish encrypted token rows from legacy plaintext rows.

Key rotation is intentionally NOT implemented in this module. A future
version will swap the single Fernet instance for MultiFernet so a new
primary key can be added without invalidating tokens encrypted under
the prior key. Sprint 6+ scope.

Operational invariant: loss of TOKEN_ENCRYPTION_KEY makes every
encrypted token permanently unrecoverable. The key is stored on Render
services (web + worker), in Apple Keychain, and on printed paper —
three places, three formats.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("cs-agent.token_crypto")

__all__ = ["encrypt", "decrypt", "InvalidToken"]


def _load_fernet() -> Fernet:
    """Read TOKEN_ENCRYPTION_KEY at import time and build the Fernet instance.

    Fails closed: any missing, empty, or malformed key raises RuntimeError
    on module load. This is intentional — silent fall-through to plaintext
    or to a default key would be far worse than refusing to start.
    """
    key = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY env var is required for token encryption. "
            "See CLAUDE.md for setup."
        )
    if len(key) != 44:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY must be a 44-char URL-safe base64 Fernet key. "
            "See CLAUDE.md for how to generate one."
        )
    try:
        instance = Fernet(key.encode("ascii"))
    except Exception as exc:
        raise RuntimeError(
            f"TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {type(exc).__name__}"
        ) from exc
    logger.info("token_crypto_initialized")
    return instance


_fernet = _load_fernet()


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns a Fernet ciphertext string (starts with 'gAAAAA')."""
    if not isinstance(plaintext, str):
        raise TypeError("encrypt() requires str input")
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string. Raises InvalidToken on failure."""
    if not isinstance(ciphertext, str):
        raise TypeError("decrypt() requires str input")
    return _fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
