"""
tests/test_connection_status_endpoint.py

Integration tests for the /app/connection-status endpoint after the
1.3 Token Expiry UX refactor. Mocks the probe layer so no live
OAuth or DB traffic happens.

Standalone-runnable:
    python3 tests/test_connection_status_endpoint.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from product.settings import DEMO_EMAIL  # noqa: E402

os.environ.setdefault("STRIPE_PRICE_ID", "price_test_fixture")

import pytest
from fastapi.testclient import TestClient

import main
from product import auth_probe


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    auth_probe._cache.clear()
    yield
    auth_probe._cache.clear()


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


# ── Endpoint shape ───────────────────────────────────────────────────────────


def test_endpoint_returns_per_provider_states():
    fake_session = {"csm_id": "user_test", "name": "Test", "email": "t@example.com"}
    fake_status = {
        "gmail":     "ok",
        "calendar":  "ok",
        "hubspot":   "missing",
        "gainsight": "expired",
        "slack":     "missing",
        "zoom":      "missing",
    }
    with patch.object(main, "_require_active_subscription", new=AsyncMock(return_value=fake_session)), \
         patch("product.auth_probe.probe_all", return_value=fake_status):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body == fake_status
    assert set(body.keys()) == {"gmail", "calendar", "hubspot", "gainsight", "slack", "zoom"}


def test_endpoint_returns_401_when_unauthenticated():
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)):
        resp = _client().get("/app/connection-status")
    assert resp.status_code == 401


def test_endpoint_returns_all_error_when_probe_raises():
    fake_session = {"csm_id": "user_test", "name": "Test", "email": "t@example.com"}
    with patch.object(main, "_require_active_subscription", new=AsyncMock(return_value=fake_session)), \
         patch("product.auth_probe.probe_all", side_effect=RuntimeError("boom")):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "gmail":     "error",
        "calendar":  "error",
        "hubspot":   "error",
        "gainsight": "error",
        "slack":     "error",
        "zoom":      "error",
    }


def test_endpoint_passes_validated_csm_id_to_probe():
    """Endpoint must validate csm_id before invoking the probe so a
    malformed Clerk-side ID does not reach the token loaders."""
    fake_session = {"csm_id": "user_validated_123", "name": "T", "email": "t@x"}
    received_id = {"value": None}

    def fake_probe(csm_id):
        received_id["value"] = csm_id
        return {"gmail": "ok", "calendar": "ok", "hubspot": "missing", "gainsight": "missing"}

    with patch.object(main, "_require_active_subscription", new=AsyncMock(return_value=fake_session)), \
         patch("product.auth_probe.probe_all", side_effect=fake_probe):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200
    assert received_id["value"] == "user_validated_123"


def test_endpoint_works_for_sarah_c_session():
    """sarah_c reaches /app/connection-status (not subscription-gated).
    Probe runs against whatever sarah_c tokens exist."""
    sarah_session = {"csm_id": "sarah_c", "name": "Sarah", "email": DEMO_EMAIL}
    fake_status = {"gmail": "ok", "calendar": "ok", "hubspot": "missing", "gainsight": "missing", "slack": "missing", "zoom": "missing"}

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=sarah_session)), \
         patch("product.auth_probe.probe_all", return_value=fake_status):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200
    assert resp.json() == fake_status


# ── Cache invalidation on reconnect ──────────────────────────────────────────


def test_probe_cache_invalidated_after_hubspot_reconnect_callback():
    """A fresh /auth/hubspot/callback success must drop the cached
    probe entry for that csm_id so the next /app/connection-status
    re-probes immediately rather than serving stale 'expired' state."""
    auth_probe.set_cached_status("user_reconnect", {"hubspot": "expired"})
    assert auth_probe.get_cached_status("user_reconnect") is not None

    auth_probe.invalidate_cache("user_reconnect")
    assert auth_probe.get_cached_status("user_reconnect") is None


# ── G.2 Stage 2: DB-backed read path ────────────────────────────────────────


def _fresh_verify_result(provider, status):
    return {
        "provider": provider,
        "status": status,
        "last_verified_at": "2099-01-01T00:00:00+00:00",
        "last_verified_age_minutes": 3,  # very fresh, < 120-min threshold
        "is_live": status == "active",
    }


def _stale_verify_result(provider, status, age_minutes=200):
    return {
        "provider": provider,
        "status": status,
        "last_verified_at": "2024-01-01T00:00:00+00:00",
        "last_verified_age_minutes": age_minutes,
        "is_live": status == "active",
    }


def _missing_verify_result(provider):
    return {
        "provider": provider,
        "status": "missing",
        "last_verified_at": None,
        "last_verified_age_minutes": None,
        "is_live": False,
    }


def test_endpoint_returns_db_backed_state_when_fresh():
    """When every present provider has fresh last_verified_at, the
    response is served from the DB without calling auth_probe.probe_all."""
    fake_session = {"csm_id": "user_db_fresh", "name": "T", "email": "t@x"}

    db_results = {
        "gmail":     _fresh_verify_result("gmail",     "active"),
        "calendar":  _fresh_verify_result("calendar",  "active"),
        "hubspot":   _fresh_verify_result("hubspot",   "expired"),
        "gainsight": _missing_verify_result("gainsight"),
        "slack":     _missing_verify_result("slack"),
        "zoom":      _missing_verify_result("zoom"),
    }

    def fake_verify(csm_id, provider):
        return db_results[provider]

    with patch.object(main, "_require_active_subscription",
                      new=AsyncMock(return_value=fake_session)), \
         patch("product.connection_service.verify_connection_live",
               side_effect=fake_verify), \
         patch("product.auth_probe.probe_all",
               side_effect=AssertionError("must not be called on the fresh path")):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200
    assert resp.json() == {
        "gmail":     "ok",
        "calendar":  "ok",
        "hubspot":   "expired",
        "gainsight": "missing",
        "slack":     "missing",
        "zoom":      "missing",
    }


def test_endpoint_falls_back_to_probe_when_db_stale():
    """A present row with age > 120 min triggers a degraded live probe."""
    fake_session = {"csm_id": "user_db_stale", "name": "T", "email": "t@x"}

    db_results = {
        "gmail":     _stale_verify_result("gmail",     "active",  age_minutes=400),
        "calendar":  _fresh_verify_result("calendar",  "active"),
        "hubspot":   _fresh_verify_result("hubspot",   "active"),
        "gainsight": _missing_verify_result("gainsight"),
        "slack":     _missing_verify_result("slack"),
        "zoom":      _missing_verify_result("zoom"),
    }

    def fake_verify(csm_id, provider):
        return db_results[provider]

    live = {
        "gmail":     "expired",  # refresh failed since last write
        "calendar":  "expired",
        "hubspot":   "ok",
        "gainsight": "missing",
        "slack":     "missing",
        "zoom":      "missing",
    }

    with patch.object(main, "_require_active_subscription",
                      new=AsyncMock(return_value=fake_session)), \
         patch("product.connection_service.verify_connection_live",
               side_effect=fake_verify), \
         patch("product.auth_probe.probe_all", return_value=live), \
         patch("product.connection_writer.write_connection_state",
               new=AsyncMock(return_value=live)):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200
    assert resp.json() == live


def test_endpoint_stale_fallback_emits_degraded_path_log(caplog):
    fake_session = {"csm_id": "user_stale_log", "name": "T", "email": "t@x"}

    db_results = {
        "gmail":     _missing_verify_result("gmail"),
        "calendar":  _missing_verify_result("calendar"),
        "hubspot":   _stale_verify_result("hubspot",   "active",  age_minutes=600),
        "gainsight": _missing_verify_result("gainsight"),
        "slack":     _missing_verify_result("slack"),
        "zoom":      _missing_verify_result("zoom"),
    }

    def fake_verify(csm_id, provider):
        return db_results[provider]

    caplog.set_level("INFO")
    with patch.object(main, "_require_active_subscription",
                      new=AsyncMock(return_value=fake_session)), \
         patch("product.connection_service.verify_connection_live",
               side_effect=fake_verify), \
         patch("product.auth_probe.probe_all",
               return_value={"gmail": "missing", "calendar": "missing",
                             "hubspot": "ok", "gainsight": "missing", "slack": "missing",
                             "zoom": "missing"}), \
         patch("product.connection_writer.write_connection_state",
               new=AsyncMock()):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "event=connection_status_stale_fallback" in m
        and "csm_id=user_stale_log" in m
        and "staleness_minutes=600" in m
        for m in msgs
    ), msgs


def test_endpoint_no_data_falls_back_to_probe_without_degraded_log(caplog):
    """A user with zero connection rows: fall back to live probe so
    sarah_c (filesystem-only tokens) and brand-new users still work,
    but do NOT emit the degraded-path log — absence of DB state is not
    a scheduler-health signal."""
    fake_session = {"csm_id": "user_no_data", "name": "T", "email": "t@x"}

    def fake_verify(csm_id, provider):
        return _missing_verify_result(provider)

    live = {
        "gmail":     "ok",
        "calendar":  "ok",
        "hubspot":   "missing",
        "gainsight": "missing",
        "slack":     "missing",
        "zoom":      "missing",
    }

    caplog.set_level("INFO")
    with patch.object(main, "_require_active_subscription",
                      new=AsyncMock(return_value=fake_session)), \
         patch("product.connection_service.verify_connection_live",
               side_effect=fake_verify), \
         patch("product.auth_probe.probe_all", return_value=live):
        resp = _client().get("/app/connection-status")

    assert resp.status_code == 200
    assert resp.json() == live
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("event=connection_status_stale_fallback" in m for m in msgs), (
        "no-data path must not log degraded-path"
    )


def test_endpoint_response_schema_matches_banner_contract():
    """ConnectionExpiryBanner reads `Object.values(data).some((v) => v
    === 'expired')`. The response must be a flat dict whose values are
    raw strings — no nested objects, no `__age_*` keys."""
    fake_session = {"csm_id": "user_schema", "name": "T", "email": "t@x"}

    db_results = {
        "gmail":     _fresh_verify_result("gmail",     "active"),
        "calendar":  _fresh_verify_result("calendar",  "active"),
        "hubspot":   _fresh_verify_result("hubspot",   "expired"),
        "gainsight": _missing_verify_result("gainsight"),
        "slack":     _missing_verify_result("slack"),
        "zoom":      _missing_verify_result("zoom"),
    }

    def fake_verify(csm_id, provider):
        return db_results[provider]

    with patch.object(main, "_require_active_subscription",
                      new=AsyncMock(return_value=fake_session)), \
         patch("product.connection_service.verify_connection_live",
               side_effect=fake_verify):
        resp = _client().get("/app/connection-status")

    body = resp.json()
    assert set(body.keys()) == {"gmail", "calendar", "hubspot", "gainsight", "slack", "zoom"}
    for v in body.values():
        assert isinstance(v, str)
        assert v in {"ok", "expired", "missing", "error"}
    # The banner check: any expired => banner shows
    assert any(v == "expired" for v in body.values())


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v", "--timeout=30"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
