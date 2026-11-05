"""
product/auth.py

Simple session-based authentication for the pilot.
In-memory session store — sessions are lost on restart, acceptable for
a single-CSM pilot.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

# In-memory session store: {session_token: {csm_id, name, email, created_at}}
_sessions: dict[str, dict] = {}

SESSION_COOKIE = "cs_agent_session"
SESSION_TTL_HOURS = 24


def create_session(csm_id: str, name: str, email: str = "") -> str:
    """Create a new session token for a CSM. Returns the token."""
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "csm_id": csm_id,
        "name": name,
        "email": email,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return token


def get_session(token: str | None) -> dict | None:
    """
    Validate a session token. Returns session dict if valid, None if invalid
    or expired. Cleans up expired sessions as a side effect.
    """
    if not token or token not in _sessions:
        return None
    session = _sessions[token]
    created = datetime.fromisoformat(session["created_at"])
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age_hours > SESSION_TTL_HOURS:
        del _sessions[token]
        return None
    return session


def delete_session(token: str | None) -> None:
    """Delete a session (logout)."""
    if token and token in _sessions:
        del _sessions[token]


def validate_credentials(csm_id: str, password: str) -> bool:
    """
    Check password against env var CSM_{CSM_ID_UPPER}_PASSWORD.
    Example: csm_id="sarah_c" → env var CSM_SARAH_C_PASSWORD
    Returns True if credentials are valid.
    """
    env_key = f"CSM_{csm_id.upper()}_PASSWORD"
    expected = os.environ.get(env_key)
    if not expected:
        return False
    return secrets.compare_digest(password, expected)


def get_csm_name(csm_id: str) -> str:
    """
    Return display name for a CSM.
    Priority: org_config → env var CSM_{ID}_NAME → csm_id fallback.
    """
    try:
        from product.org_config import get_csm_config
        cfg = get_csm_config(csm_id)
        if cfg.get("name"):
            return cfg["name"]
    except Exception:
        pass
    env_key = f"CSM_{csm_id.upper()}_NAME"
    return os.environ.get(env_key, csm_id)


def get_csm_email(csm_id: str) -> str:
    """
    Return email for a CSM.
    Priority: org_config → env var CSM_{ID}_EMAIL → empty string.
    """
    try:
        from product.org_config import get_csm_config
        cfg = get_csm_config(csm_id)
        if cfg.get("email"):
            return cfg["email"]
    except Exception:
        pass
    env_key = f"CSM_{csm_id.upper()}_EMAIL"
    return os.environ.get(env_key, "")
