"""
product/demo_identity.py

Single source of truth for recognising the scripted /demo account.

The demo account (settings.DEMO_CSM_ID / settings.DEMO_EMAIL) bypasses
trial provisioning and billing. That bypass must only ever match the real
demo address, so the check is an exact, case-insensitive,
whitespace-trimmed comparison. Substring checks (DEMO_EMAIL in email) would
let lookalikes such as "x" + DEMO_EMAIL or DEMO_EMAIL + ".example.org"
through.

Every demo path is gated by DEMO_MODE (product.settings.demo_mode_enabled).
With DEMO_MODE off, the demo address and the sarah_c id are treated as
ordinary users.
"""

from __future__ import annotations

from product.settings import DEMO_CSM_ID, DEMO_EMAIL, demo_mode_enabled

__all__ = ["DEMO_CSM_ID", "DEMO_EMAIL", "is_demo_csm_id", "is_demo_email"]


def is_demo_email(email: str | None) -> bool:
    """True only if DEMO_MODE is on and `email` is exactly the demo address
    (case/whitespace-insensitive)."""
    if not demo_mode_enabled():
        return False
    if not email or not isinstance(email, str):
        return False
    return email.strip().lower() == DEMO_EMAIL


def is_demo_csm_id(csm_id: str | None) -> bool:
    """True only if DEMO_MODE is on and `csm_id` is the demo identity."""
    return demo_mode_enabled() and csm_id == DEMO_CSM_ID
