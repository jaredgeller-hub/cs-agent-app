"""
tests/test_resolve_auth_org_config.py

_resolve_auth (main.py) maps a verified Clerk user to a csm_id via
org_config.json. These tests pin how it behaves when that file is absent,
present, or broken:

  - Fresh self-hosted install (no org_config.json, no pilot_config.json,
    DEMO_MODE and BILLING_ENABLED off): a signed-in Clerk user reaches the
    app with their Clerk user ID as identity. Before the fix every app
    route returned 401.
  - No Clerk sign-in still gets 401, with or without a config file.
  - With org_config.json present (production): a listed user maps to the
    org_config key, an unlisted user maps to their Clerk user ID. Unchanged.
  - A config file that exists but is malformed is not treated as empty:
    the Clerk session is refused (fail closed).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
import product.org_config as org_config

UNLISTED = {"user_id": "user_SelfHostUnlisted000000000", "email": "csm@acme.test", "first_name": "Ana"}
# Listed in org_config.example.json under this key, with this email.
LISTED_KEY = "user_ExampleDemoCsm0000000000000"
LISTED = {"user_id": LISTED_KEY, "email": "demo@example.com", "first_name": "Sarah"}

APP_ROUTES = ["/app/subscription-status", "/app/accounts", "/app/connection-status"]


def _fake_clerk(user):
    async def _verify(request):
        return dict(user)
    return _verify


async def _no_clerk(request):
    raise HTTPException(status_code=401, detail="Not authenticated")


@pytest.fixture
def no_config(tmp_path, monkeypatch):
    """Neither config file exists. Names match the examples so the conftest
    guard is satisfied; monkeypatch restores the real example paths after."""
    monkeypatch.setattr(org_config, "_ORG_CONFIG_PATH", tmp_path / "org_config.example.json")
    monkeypatch.setattr(org_config, "_PILOT_CONFIG_PATH", tmp_path / "pilot_config.example.json")
    monkeypatch.setattr(org_config, "_cached_config", None)
    yield
    org_config._cached_config = None


@pytest.fixture
def example_config(monkeypatch):
    monkeypatch.setattr(org_config, "_cached_config", None)
    yield
    org_config._cached_config = None


@pytest.fixture
def self_host_flags(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("BILLING_ENABLED", "false")


def _resolve(monkeypatch, verify):
    monkeypatch.setattr(main, "verify_clerk_token", verify)
    return asyncio.run(main._resolve_auth(object()))


# ── Fresh self-hosted install: no config file ───────────────────────────────

def test_missing_config_raises_dedicated_error(no_config):
    with pytest.raises(org_config.OrgConfigMissingError):
        org_config.list_active_csms()
    # Still a FileNotFoundError, so existing callers are unaffected.
    assert issubclass(org_config.OrgConfigMissingError, FileNotFoundError)


def test_no_config_helpers_treat_absence_as_empty(no_config):
    # Used by /app/accounts and the digest pipeline after auth succeeds.
    assert org_config.try_get_csm_config(UNLISTED["user_id"]) is None
    assert org_config.get_pilot_mode() == "live"


def test_malformed_config_helpers_still_raise(tmp_path, monkeypatch):
    bad = tmp_path / "org_config.example.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(org_config, "_ORG_CONFIG_PATH", bad)
    monkeypatch.setattr(org_config, "_cached_config", None)
    with pytest.raises(ValueError):
        org_config.try_get_csm_config("anyone")
    with pytest.raises(ValueError):
        org_config.get_pilot_mode()


def test_no_config_clerk_user_gets_200_on_app_routes(no_config, self_host_flags, monkeypatch):
    monkeypatch.setattr(main, "verify_clerk_token", _fake_clerk(UNLISTED))
    client = TestClient(main.app)
    for path in APP_ROUTES:
        resp = client.get(path)
        assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:200]}"


def test_no_config_identity_is_clerk_user_id(no_config, self_host_flags, monkeypatch):
    session = _resolve(monkeypatch, _fake_clerk(UNLISTED))
    assert session == {"csm_id": UNLISTED["user_id"], "name": "Ana", "email": "csm@acme.test"}


def test_no_config_no_clerk_gets_401(no_config, self_host_flags, monkeypatch):
    monkeypatch.setattr(main, "verify_clerk_token", _no_clerk)
    client = TestClient(main.app)
    for path in APP_ROUTES:
        resp = client.get(path)
        assert resp.status_code == 401, f"{path}: {resp.status_code}"


# ── Config present (production): unchanged ─────────────────────────────────

def test_with_config_listed_user_maps_to_org_config_key(example_config, monkeypatch):
    session = _resolve(monkeypatch, _fake_clerk(LISTED))
    assert session["csm_id"] == LISTED_KEY
    assert session["name"] == "Sarah C."  # name from org_config wins
    assert session["email"] == "demo@example.com"


def test_with_config_unlisted_user_maps_to_clerk_user_id(example_config, monkeypatch):
    session = _resolve(monkeypatch, _fake_clerk(UNLISTED))
    assert session == {"csm_id": UNLISTED["user_id"], "name": "Ana", "email": "csm@acme.test"}


def test_with_config_no_clerk_gets_401(example_config, self_host_flags, monkeypatch):
    monkeypatch.setattr(main, "verify_clerk_token", _no_clerk)
    client = TestClient(main.app)
    for path in APP_ROUTES:
        assert client.get(path).status_code == 401, path


# ── Broken config: fail closed ──────────────────────────────────────────────

@pytest.mark.parametrize("content", [
    "{not json",                                   # JSONDecodeError (ValueError)
    json.dumps({"org_name": "x"}),                 # missing required fields
    json.dumps({"org_name": "x", "crm_type": "hubspot",
                "timezone": "UTC", "csms": []}),   # csms not a dict
])
def test_malformed_config_refuses_clerk_session(tmp_path, monkeypatch, self_host_flags, content):
    bad = tmp_path / "org_config.example.json"
    bad.write_text(content, encoding="utf-8")
    monkeypatch.setattr(org_config, "_ORG_CONFIG_PATH", bad)
    monkeypatch.setattr(org_config, "_cached_config", None)
    try:
        assert _resolve(monkeypatch, _fake_clerk(UNLISTED)) is None
        client = TestClient(main.app)
        assert client.get("/app/accounts").status_code == 401
    finally:
        org_config._cached_config = None
