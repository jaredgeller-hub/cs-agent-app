"""
product/settings.py

Deployment settings read from the environment, defined once and imported
everywhere else. Nothing outside this module should hardcode the
production base URL or the Anthropic model id
(tests/test_settings.py enforces this).

  ANTHROPIC_MODEL   Claude model id for all LLM calls.
                    Default: claude-sonnet-4-6.
  PUBLIC_BASE_URL   Public origin of this deployment, e.g. https://example.com.
                    Used to build links, runtime OAuth callback defaults,
                    emails and digests. Default: http://localhost:8000.
  DEMO_MODE         Enables the scripted /demo experience and every
                    demo-account (sarah_c / demo email) path. Default: off.
  BILLING_ENABLED   Enables the Stripe subscription gate, checkout, portal,
                    webhook and trial lifecycle jobs. Default: off. When off,
                    the app never calls Stripe and never gates on
                    subscription state.

  DEMO_EMAIL        Email address of the demo account. Default demo@example.com.
  DEMO_CSM_ID       csm_id of the demo identity. Default demo_user.
  SUPPORT_EMAIL     Contact address shown to users and used for operator
                    alerts. Default support@example.com.
  FROM_EMAIL        Sender address for system emails. Default
                    noreply@example.com.
  COMPANY_NAME      Operator's company name in page titles and emails.
                    Default "Your Company".
  COMPANY_LEGAL_NAME Legal entity name in page metadata. Default COMPANY_NAME.
  COMPANY_DOMAIN    Operator's company website domain named on the security
                    and accessibility pages. Default example.com.
  SENDER_NAME       Signature name on trial emails. Default "The team".
  SENDER_TITLE      Optional title shown before COMPANY_NAME in the first
                    trial email's signature. Default empty.
  WAITLIST_FORM_URL Target of the landing page "Join the waitlist" buttons.
                    Default mailto:SUPPORT_EMAIL.

The string settings above (and ANTHROPIC_MODEL, PUBLIC_BASE_URL) are read
once at import. DEMO_MODE and
BILLING_ENABLED are read on every call so they can be toggled in tests.
"""

from __future__ import annotations

import os

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_PUBLIC_BASE_URL = "http://localhost:8000"

_TRUTHY = {"1", "true", "yes", "on"}


def _str_env(name: str, default: str) -> str:
    value = (os.getenv(name) or "").strip()
    return value or default


ANTHROPIC_MODEL: str = _str_env("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)

PUBLIC_BASE_URL: str = _str_env("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")


DEFAULT_DEMO_EMAIL = "demo@example.com"
DEFAULT_DEMO_CSM_ID = "demo_user"
DEFAULT_SUPPORT_EMAIL = "support@example.com"
DEFAULT_FROM_EMAIL = "noreply@example.com"
DEFAULT_COMPANY_NAME = "Your Company"
DEFAULT_SENDER_NAME = "The team"
DEFAULT_COMPANY_DOMAIN = "example.com"

# Lower-cased and trimmed so is_demo_email()'s exact match is case-insensitive.
DEMO_EMAIL: str = _str_env("DEMO_EMAIL", DEFAULT_DEMO_EMAIL).lower()
DEMO_CSM_ID: str = _str_env("DEMO_CSM_ID", DEFAULT_DEMO_CSM_ID)
SUPPORT_EMAIL: str = _str_env("SUPPORT_EMAIL", DEFAULT_SUPPORT_EMAIL)
FROM_EMAIL: str = _str_env("FROM_EMAIL", DEFAULT_FROM_EMAIL)
COMPANY_NAME: str = _str_env("COMPANY_NAME", DEFAULT_COMPANY_NAME)
COMPANY_LEGAL_NAME: str = _str_env("COMPANY_LEGAL_NAME", COMPANY_NAME)
COMPANY_DOMAIN: str = _str_env("COMPANY_DOMAIN", DEFAULT_COMPANY_DOMAIN)
SENDER_NAME: str = _str_env("SENDER_NAME", DEFAULT_SENDER_NAME)
SENDER_TITLE: str = (os.getenv("SENDER_TITLE") or "").strip()
WAITLIST_FORM_URL: str = _str_env("WAITLIST_FORM_URL", f"mailto:{SUPPORT_EMAIL}")


def public_url(path: str = "") -> str:
    """Absolute URL on this deployment. `path` should start with '/'."""
    if not path:
        return PUBLIC_BASE_URL
    if not path.startswith("/"):
        path = "/" + path
    return PUBLIC_BASE_URL + path


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUTHY


def demo_mode_enabled() -> bool:
    """True only when DEMO_MODE is explicitly set to a truthy value."""
    return _flag("DEMO_MODE")


def billing_enabled() -> bool:
    """True only when BILLING_ENABLED is explicitly set to a truthy value."""
    return _flag("BILLING_ENABLED")
