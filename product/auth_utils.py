"""
product/auth_utils.py

Shared Google OAuth credential loading used by gmail.py, calendar.py, and delivery.py.

Stage 2a: filesystem fallback removed. Postgres is the only token store.
Refreshed credentials are written back via product.token_store.save_token,
which encrypts at the application layer.
"""

from __future__ import annotations

import json
import logging

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

logger = logging.getLogger("cs-agent.auth_utils")


def load_google_credentials(csm_id: str) -> Credentials:
    """Load and refresh Google OAuth credentials for a CSM.

    Reads from Postgres only. Refreshed tokens are written back to
    Postgres via save_token (which encrypts).
    """
    from product.csm_id_validation import validate_csm_id
    csm_id = validate_csm_id(csm_id)

    from product.auth_google import load_google_token
    token_data = load_google_token(csm_id)
    if not token_data:
        raise FileNotFoundError(
            f"No Google token found for {csm_id} (Postgres-only storage)."
        )

    creds = Credentials.from_authorized_user_info(token_data)

    if creds.expired and creds.refresh_token:
        from product.token_lock import token_lock
        with token_lock(csm_id, "google"):
            # Double-check inside the lock — another caller may have refreshed
            # while we were waiting. Use the SAME threshold as the pre-lock check
            # (creds.expired = strict, no buffer) by re-reading + reconstructing.
            fresh_token = load_google_token(csm_id)
            if fresh_token:
                creds = Credentials.from_authorized_user_info(fresh_token)
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    logger.error("Google token refresh failed for %s: %s", csm_id, exc)
                    raise
                refreshed = json.loads(creds.to_json())
                from product.token_store import save_token
                save_token(csm_id, "google", refreshed)

    return creds
