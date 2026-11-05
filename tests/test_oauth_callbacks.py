"""
tests/test_oauth_callbacks.py

Covers the three OAuth callback handlers in main.py:
  - /auth/google/callback    (google_auth_callback)
  - /auth/hubspot/callback   (hubspot_auth_callback)
  - /auth/gainsight/callback (gainsight_auth_callback)

Two paths per handler:
  - Cancel/error path: provider redirects with error=access_denied (and
    no code). Handler returns 200 + HTML that posts oauth_canceled to
    the opener. Three sub-cases per handler:
      a. valid state    -> provider attribution comes from state lookup
      b. missing state  -> provider attribution falls back to handler default
      c. expired state  -> provider attribution falls back to handler default
  - Success path: provider redirects with valid code + state. Handler
    exchanges the code, persists the token, and returns 200 + HTML that
    posts oauth_complete. Previously uncovered; added as a regression net
    while the file was being touched.

Total: 12 tests (3 cancel + 1 success per provider).

Mocking strategy (mirrors tests/test_chat_thread_id_emission.py):
  - main._get_oauth_state_with_status -> patched to drive the (entry, status)
    return tuple per test.
  - main.exchange_<provider>_code     -> patched on success-path tests to
    return a fake token dict without making real network calls.
  - main.save_<provider>_token        -> patched on success-path tests to
    no-op without touching Postgres or the filesystem.
  - product.token_store.save_token    -> patched on Google success path so
    the gmail shadow-row write inside the try/except is a no-op.

Standalone-runnable:
    python3 tests/test_oauth_callbacks.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


PROVIDERS = [
    ("google",    "/auth/google/callback",    "exchange_google_code",    "save_google_token"),
    ("hubspot",   "/auth/hubspot/callback",   "exchange_hubspot_code",   "save_hubspot_token"),
    ("gainsight", "/auth/gainsight/callback", "exchange_gainsight_code", "save_gainsight_token"),
]


# Cancel path: state row exists and is valid. provider_label should come
# from the state entry. We set entry["provider"] to the same provider so
# the assertion is consistent regardless of which lookup branch wins.
@pytest.mark.parametrize("provider,route,_exch,_save", PROVIDERS)
def test_cancel_with_valid_state(provider, route, _exch, _save):
    fake_entry = {"csm_id": "user_test", "code_verifier": "v", "provider": provider}
    with patch.object(main, "_get_oauth_state_with_status", return_value=(fake_entry, "ok")):
        resp = client.get(route, params={"state": "valid_state", "error": "access_denied"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "oauth_canceled" in body
    assert f'provider: "{provider}"' in body
    assert 'reason: "access_denied"' in body
    # Sanity: window.location.origin target preserved (no wildcard regression)
    assert 'window.location.origin' in body
    assert ', "*"' not in body


# Cancel path: state query param is absent. Provider attribution falls
# back to the handler's hardcoded default (google/hubspot/gainsight).
@pytest.mark.parametrize("provider,route,_exch,_save", PROVIDERS)
def test_cancel_with_missing_state(provider, route, _exch, _save):
    # No state lookup should be reached, but patch defensively so the test
    # fails loudly if the handler regresses to calling it with state=None.
    with patch.object(main, "_get_oauth_state_with_status") as mock_lookup:
        resp = client.get(route, params={"error": "access_denied"})
        mock_lookup.assert_not_called()
    assert resp.status_code == 200
    body = resp.text
    assert "oauth_canceled" in body
    assert f'provider: "{provider}"' in body
    assert 'reason: "access_denied"' in body


# Cancel path: state was provided but the row has expired (or the lookup
# returned a non-ok status). Provider attribution falls back to default.
@pytest.mark.parametrize("provider,route,_exch,_save", PROVIDERS)
def test_cancel_with_expired_state(provider, route, _exch, _save):
    with patch.object(main, "_get_oauth_state_with_status", return_value=(None, "expired")):
        resp = client.get(route, params={"state": "stale_state", "error": "access_denied"})
    assert resp.status_code == 200
    body = resp.text
    assert "oauth_canceled" in body
    assert f'provider: "{provider}"' in body
    assert 'reason: "access_denied"' in body


# Success path: code + state both present, lookup ok. exchange_<provider>_code
# and save_<provider>_token are mocked. Asserts the existing oauth_complete
# postMessage HTML still ships unmodified.
@pytest.mark.parametrize("provider,route,exch_attr,save_attr", PROVIDERS)
def test_success_path(provider, route, exch_attr, save_attr):
    fake_entry = {"csm_id": "user_test", "code_verifier": "v", "provider": provider}
    fake_token = {"access_token": "fake_token", "scopes": ["scope_a"]}

    patches = [
        patch.object(main, "_get_oauth_state_with_status", return_value=(fake_entry, "ok")),
        patch.object(main, exch_attr, return_value=fake_token),
        patch.object(main, save_attr, return_value=None),
    ]
    if provider == "google":
        # Google success path also writes a gmail shadow row via
        # token_store.save_token inside a try/except. Patch it so the test
        # does not require a live database session.
        patches.append(patch("product.token_store.save_token", return_value=None))

    with patches[0], patches[1], patches[2], (patches[3] if len(patches) == 4 else _nullcontext()):
        resp = client.get(route, params={"code": "fake_code", "state": "valid_state"})

    assert resp.status_code == 200
    body = resp.text
    assert "oauth_complete" in body
    assert f'provider: "{provider}"' in body
    assert 'window.location.origin' in body


class _nullcontext:
    """Trivial null context manager for the conditional fourth patch above.
    contextlib.nullcontext exists in stdlib but the test stays explicit
    so the structure of the success-path setup is obvious to a future reader."""
    def __enter__(self):
        return None
    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
