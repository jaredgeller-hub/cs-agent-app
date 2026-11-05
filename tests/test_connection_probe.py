"""
tests/test_connection_probe.py

Unit tests for product/auth_probe.py. Mocks token loaders and refresh
calls so no live OAuth traffic happens.

Covers:
  - Each provider state transition (ok, expired, missing, error)
  - Best-of-2 retry on transient failures
  - 60s cache TTL behavior
  - Cache invalidation on reconnect

Standalone-runnable:
    python3 tests/test_connection_probe.py
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product import auth_probe


@pytest.fixture(autouse=True)
def _clear_cache():
    auth_probe._cache.clear()
    yield
    auth_probe._cache.clear()


# ── Cache behavior ───────────────────────────────────────────────────────────


def test_cache_returns_cached_within_ttl():
    auth_probe.set_cached_status("user_x", {"gmail": "ok", "calendar": "ok",
                                              "hubspot": "missing", "gainsight": "missing"})
    cached = auth_probe.get_cached_status("user_x")
    assert cached is not None
    assert cached["gmail"] == "ok"


def test_cache_expires_after_ttl():
    with patch.object(auth_probe, "_CACHE_TTL_S", 0.05):
        auth_probe.set_cached_status("user_x", {"gmail": "ok"})
        assert auth_probe.get_cached_status("user_x") is not None
        time.sleep(0.1)
        assert auth_probe.get_cached_status("user_x") is None


def test_invalidate_cache_drops_entry():
    auth_probe.set_cached_status("user_x", {"gmail": "ok"})
    auth_probe.invalidate_cache("user_x")
    assert auth_probe.get_cached_status("user_x") is None


def test_invalidate_cache_safe_on_unknown_user():
    auth_probe.invalidate_cache("never_seen")  # must not raise


# ── HubSpot probe states ─────────────────────────────────────────────────────


def test_hubspot_missing_when_no_token():
    with patch("product.connectors.hubspot_mcp.load_hubspot_token", return_value=None):
        assert auth_probe._probe_hubspot("user_x") == "missing"


def test_hubspot_ok_when_token_fresh():
    fresh_token = {
        "access_token":  "abc",
        "refresh_token": "rt",
        "expires_at":    time.time() + 3600,
    }
    with patch("product.connectors.hubspot_mcp.load_hubspot_token", return_value=fresh_token):
        assert auth_probe._probe_hubspot("user_x") == "ok"


def test_hubspot_ok_after_successful_refresh():
    near_expiry_token = {
        "access_token":  "abc",
        "refresh_token": "rt_valid",
        "expires_at":    time.time() - 10,
    }
    with patch("product.connectors.hubspot_mcp.load_hubspot_token", return_value=near_expiry_token), \
         patch("product.connectors.hubspot_mcp.refresh_hubspot_token", return_value={"access_token": "new"}):
        assert auth_probe._probe_hubspot("user_x") == "ok"


def test_hubspot_expired_on_revoked_refresh_token():
    token = {
        "access_token":  "abc",
        "refresh_token": "rt_revoked",
        "expires_at":    time.time() - 10,
    }
    with patch("product.connectors.hubspot_mcp.load_hubspot_token", return_value=token), \
         patch("product.connectors.hubspot_mcp.refresh_hubspot_token",
               side_effect=ValueError("400 Bad Request: refresh_token invalid")):
        assert auth_probe._probe_hubspot("user_x") == "expired"


def test_hubspot_expired_when_no_refresh_token_field():
    token = {
        "access_token": "abc",
        "expires_at":   time.time() - 10,
    }
    with patch("product.connectors.hubspot_mcp.load_hubspot_token", return_value=token):
        assert auth_probe._probe_hubspot("user_x") == "expired"


def test_hubspot_best_of_2_recovers_from_transient():
    """First refresh raises a non-classifiable transient, second succeeds."""
    token = {
        "access_token":  "abc",
        "refresh_token": "rt_valid",
        "expires_at":    time.time() - 10,
    }
    side_effects = [TimeoutError("timeout"), {"access_token": "new"}]
    with patch("product.connectors.hubspot_mcp.load_hubspot_token", return_value=token), \
         patch("product.connectors.hubspot_mcp.refresh_hubspot_token", side_effect=side_effects):
        assert auth_probe._probe_hubspot("user_x") == "ok"


def test_hubspot_error_after_two_transient_failures():
    token = {
        "access_token":  "abc",
        "refresh_token": "rt_valid",
        "expires_at":    time.time() - 10,
    }
    with patch("product.connectors.hubspot_mcp.load_hubspot_token", return_value=token), \
         patch("product.connectors.hubspot_mcp.refresh_hubspot_token",
               side_effect=TimeoutError("timeout")):
        assert auth_probe._probe_hubspot("user_x") == "error"


# ── Gainsight probe states ───────────────────────────────────────────────────


def test_gainsight_missing_when_no_token():
    with patch("product.connectors.gainsight_mcp.load_gainsight_token", return_value=None):
        assert auth_probe._probe_gainsight("user_x") == "missing"


def test_gainsight_ok_when_fresh():
    fresh_token = {
        "access_token":  "abc",
        "refresh_token": "rt",
        "expires_at":    time.time() + 3600,
    }
    with patch("product.connectors.gainsight_mcp.load_gainsight_token", return_value=fresh_token):
        assert auth_probe._probe_gainsight("user_x") == "ok"


def test_gainsight_expired_on_revoked():
    token = {
        "access_token":  "abc",
        "refresh_token": "rt_revoked",
        "expires_at":    time.time() - 10,
    }
    with patch("product.connectors.gainsight_mcp.load_gainsight_token", return_value=token), \
         patch("product.connectors.gainsight_mcp.refresh_gainsight_token",
               side_effect=ValueError("invalid refresh_Token")):
        assert auth_probe._probe_gainsight("user_x") == "expired"


# ── Google probe states ──────────────────────────────────────────────────────


def test_google_ok_when_creds_not_expired():
    creds = MagicMock()
    creds.expired = False
    creds.refresh_token = "rt"
    with patch("product.auth_google.load_google_token", return_value={"refresh_token": "rt"}), \
         patch("product.auth_utils.load_google_credentials", return_value=creds):
        assert auth_probe._probe_google("user_x") == "ok"


def test_google_missing_when_filenotfound():
    with patch("product.auth_google.load_google_token", return_value=None), \
         patch("product.auth_utils.load_google_credentials",
               side_effect=FileNotFoundError("no token")):
        # Token loader returns None and credential loader signals not-found.
        # Even though we hit the credentials path, FileNotFoundError yields "missing".
        # First check: if load_google_token returns None AND no filesystem candidates,
        # we short-circuit to "missing" via the candidates check.
        # We patch safe_data_file to always point at a non-existent path so the
        # short-circuit fires.
        with patch("product.csm_id_validation.safe_data_file") as mock_safe:
            mock_path = MagicMock()
            mock_path.exists.return_value = False
            mock_safe.return_value = mock_path
            assert auth_probe._probe_google("user_x") == "missing"


def test_google_expired_on_refresh_error():
    from google.auth.exceptions import RefreshError
    creds = MagicMock()
    creds.expired = True
    creds.refresh_token = "rt"
    creds.refresh.side_effect = RefreshError("token revoked")
    with patch("product.auth_google.load_google_token", return_value={"refresh_token": "rt"}), \
         patch("product.auth_utils.load_google_credentials", return_value=creds):
        assert auth_probe._probe_google("user_x") == "expired"


def test_google_expired_when_no_refresh_token():
    creds = MagicMock()
    creds.expired = True
    creds.refresh_token = None
    with patch("product.auth_google.load_google_token", return_value={"refresh_token": ""}), \
         patch("product.auth_utils.load_google_credentials", return_value=creds):
        assert auth_probe._probe_google("user_x") == "expired"


# ── probe_all integration ────────────────────────────────────────────────────


def test_probe_all_returns_five_keys_with_states():
    creds = MagicMock()
    creds.expired = False
    creds.refresh_token = "rt"
    with patch("product.auth_google.load_google_token", return_value={"refresh_token": "rt"}), \
         patch("product.auth_utils.load_google_credentials", return_value=creds), \
         patch("product.connectors.hubspot_mcp.load_hubspot_token", return_value=None), \
         patch("product.connectors.gainsight_mcp.load_gainsight_token", return_value=None), \
         patch("product.connectors.slack_mcp.load_slack_token", return_value=None), \
         patch("product.connectors.zoom_mcp.load_zoom_token", return_value=None):
        result = auth_probe.probe_all("user_x")

    assert set(result.keys()) == {"gmail", "calendar", "hubspot", "gainsight", "slack", "zoom"}
    assert result["gmail"] == "ok"
    assert result["calendar"] == "ok"
    assert result["hubspot"] == "missing"
    assert result["gainsight"] == "missing"
    assert result["slack"] == "missing"
    assert result["zoom"] == "missing"


def test_probe_all_uses_cache():
    """Second call within TTL must not invoke loaders again."""
    load_count = {"n": 0}

    def fake_loader(_csm):
        load_count["n"] += 1
        return None

    with patch("product.auth_google.load_google_token", side_effect=fake_loader), \
         patch("product.auth_utils.load_google_credentials",
               side_effect=FileNotFoundError("no token")), \
         patch("product.csm_id_validation.safe_data_file") as mock_safe, \
         patch("product.connectors.hubspot_mcp.load_hubspot_token", side_effect=fake_loader), \
         patch("product.connectors.gainsight_mcp.load_gainsight_token", side_effect=fake_loader):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        mock_safe.return_value = mock_path

        auth_probe.probe_all("user_x")
        first_count = load_count["n"]

        auth_probe.probe_all("user_x")  # second call hits cache
        second_count = load_count["n"]

    assert second_count == first_count, (
        f"Second probe_all call invoked loaders ({second_count - first_count} extra calls); "
        "should have hit the cache"
    )


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v", "--timeout=30"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
