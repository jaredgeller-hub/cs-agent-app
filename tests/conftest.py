"""
tests/conftest.py

Pytest auto-discovers this file before collecting any test modules.
Anything at module top runs before test files import product modules.

product/token_crypto.py reads TOKEN_ENCRYPTION_KEY at import time and
raises RuntimeError if it's missing or malformed. Test files indirectly
import token_crypto (via product.token_store etc.) during collection,
so the env var must be set HERE at module top — not inside a fixture,
which runs too late.

The key below is a test-only Fernet key. It is intentionally distinct
from the dev-only key documented in CLAUDE.md and from the production
key on Render. Do not reuse this key for any other purpose.
"""

import os

os.environ.setdefault(
    "TOKEN_ENCRYPTION_KEY",
    "DdTesYfAw9ieKl1973x-OoRZMALZTv4jLR3Jctt0gKc=",
)

# The existing suite pins today's production behaviour, where the demo and
# billing are both on (Render sets DEMO_MODE=true and BILLING_ENABLED=true).
# Both default OFF in the app; tests that cover the off state set them
# explicitly with monkeypatch (see tests/test_settings.py).
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("BILLING_ENABLED", "true")
# Production's demo identity id. Fictional, not personal data; the demo
# EMAIL is deliberately left at its neutral default (settings.DEMO_EMAIL).
os.environ.setdefault("DEMO_CSM_ID", "sarah_c")

# Tests read the committed example configs, never the deployment's real
# org_config.json / pilot_config.json (which hold production identities).
from pathlib import Path as _Path  # noqa: E402
import product.org_config as _org_config  # noqa: E402

_REPO = _Path(__file__).resolve().parent.parent
_org_config._ORG_CONFIG_PATH = _REPO / "org_config.example.json"
_org_config._PILOT_CONFIG_PATH = _REPO / "pilot_config.example.json"
_org_config._cached_config = None


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _assert_example_org_config():
    """Fail any test that re-points org_config back at the real files."""
    yield
    assert _org_config._ORG_CONFIG_PATH.name == "org_config.example.json"
    assert _org_config._PILOT_CONFIG_PATH.name == "pilot_config.example.json"
