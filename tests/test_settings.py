"""
tests/test_settings.py

Covers product/settings.py and the four settings it owns:

  ANTHROPIC_MODEL   default vs. override
  PUBLIC_BASE_URL   default vs. override; used by OAuth defaults and links
  DEMO_MODE         off: demo address / sarah_c are ordinary users, demo
                    routes 404, no unauthenticated demo /chat, no legacy
                    session or demo API key. On: demo behaves as today.
  BILLING_ENABLED   off: no Stripe calls, no subscription lockout, trial
                    jobs skipped. On: today's gate, still failing closed.

Plus a guard that fails if a hardcoded production URL or model id appears
outside product/settings.py.

tests/conftest.py defaults DEMO_MODE and BILLING_ENABLED to true for the
rest of the suite; every test here sets them explicitly.

Standalone-runnable:
    python3 tests/test_settings.py
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
import product.settings as settings
from product.database import Base, Customer
from product.demo_identity import is_demo_csm_id, is_demo_email

_test_engine = create_engine(
    "sqlite:///:memory:",
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _bind_session_to_this_files_engine():
    _db.SessionLocal = _TestSession
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


import main  # noqa: E402
import stripe  # noqa: E402

from product.settings import DEMO_EMAIL  # noqa: E402


def _demo(monkeypatch, on: bool):
    monkeypatch.setenv("DEMO_MODE", "true" if on else "false")


def _billing(monkeypatch, on: bool):
    monkeypatch.setenv("BILLING_ENABLED", "true" if on else "false")


def _row(clerk_user_id: str) -> Customer | None:
    s = _TestSession()
    try:
        return s.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
    finally:
        s.close()


def _add_customer(clerk_user_id: str, status: str, trial_ends_at=None):
    s = _TestSession()
    try:
        s.add(Customer(
            id=clerk_user_id, email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id, subscription_status=status,
            trial_ends_at=trial_ends_at,
        ))
        s.commit()
    finally:
        s.close()


@pytest.fixture
def no_stripe(monkeypatch):
    """Replace every Stripe entry point the app uses with a recording mock."""
    mocks = {
        "checkout": MagicMock(side_effect=AssertionError("Stripe called")),
        "portal": MagicMock(side_effect=AssertionError("Stripe called")),
        "webhook": MagicMock(side_effect=AssertionError("Stripe called")),
        "balance": MagicMock(side_effect=AssertionError("Stripe called")),
        "customer_list": MagicMock(side_effect=AssertionError("Stripe called")),
    }
    monkeypatch.setattr(stripe.checkout.Session, "create", mocks["checkout"])
    monkeypatch.setattr(stripe.billing_portal.Session, "create", mocks["portal"])
    monkeypatch.setattr(stripe.Webhook, "construct_event", mocks["webhook"])
    monkeypatch.setattr(stripe.Balance, "retrieve", mocks["balance"])
    monkeypatch.setattr(stripe.Customer, "list", mocks["customer_list"])
    return mocks


def _assert_no_stripe(mocks):
    for name, m in mocks.items():
        assert m.call_count == 0, f"Stripe {name} was called"


# ── ANTHROPIC_MODEL / PUBLIC_BASE_URL ────────────────────────────────────────

@pytest.fixture
def reload_settings(monkeypatch):
    def _reload():
        return importlib.reload(settings)
    yield _reload
    monkeypatch.undo()
    importlib.reload(settings)


def test_anthropic_model_unset_uses_default(monkeypatch, reload_settings):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    s = reload_settings()
    assert s.ANTHROPIC_MODEL == "claude-sonnet-4-6"
    monkeypatch.setenv("ANTHROPIC_MODEL", "   ")
    assert reload_settings().ANTHROPIC_MODEL == "claude-sonnet-4-6"


def test_anthropic_model_set_uses_value(monkeypatch, reload_settings):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test-model")
    assert reload_settings().ANTHROPIC_MODEL == "claude-test-model"


def test_model_is_imported_from_settings_everywhere():
    import product.digest.builder as b
    import product.digest.generator as g
    import product.postcall.builder as p
    for mod in (b, g, p):
        assert mod.ANTHROPIC_MODEL == settings.ANTHROPIC_MODEL
    assert main.MODEL == settings.ANTHROPIC_MODEL


def test_public_base_url_default_and_override(monkeypatch, reload_settings):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    s = reload_settings()
    assert s.PUBLIC_BASE_URL == "http://localhost:8000"
    assert s.public_url("/app") == "http://localhost:8000/app"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test/")
    s = reload_settings()
    assert s.PUBLIC_BASE_URL == "https://example.test"
    assert s.public_url("/auth/google/callback") == "https://example.test/auth/google/callback"
    assert s.public_url("app") == "https://example.test/app"


def test_oauth_redirect_defaults_follow_public_base_url():
    import product.auth_google as ag
    import product.connectors.gainsight_mcp as gs
    import product.connectors.hubspot_mcp as hs
    import product.connectors.slack_mcp as sl
    import product.connectors.zoom_mcp as zm
    for mod, prov in ((ag, "google"), (gs, "gainsight"), (hs, "hubspot"), (sl, "slack"), (zm, "zoom")):
        assert mod._DEFAULT_REDIRECT_URI == f"{settings.PUBLIC_BASE_URL}/auth/{prov}/callback"


def test_flags_default_off(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("BILLING_ENABLED", raising=False)
    assert settings.demo_mode_enabled() is False
    assert settings.billing_enabled() is False
    for val in ("true", "TRUE", "1", "yes", "on"):
        monkeypatch.setenv("DEMO_MODE", val)
        assert settings.demo_mode_enabled() is True
    for val in ("false", "0", "", "no", "off", "maybe"):
        monkeypatch.setenv("BILLING_ENABLED", val)
        assert settings.billing_enabled() is False


# ── DEMO_MODE ────────────────────────────────────────────────────────────────

def test_demo_off_demo_identity_is_ordinary(monkeypatch):
    _demo(monkeypatch, False)
    assert is_demo_email(DEMO_EMAIL) is False
    assert is_demo_email(f"  {DEMO_EMAIL.upper()} ") is False
    assert is_demo_csm_id("sarah_c") is False


def test_demo_on_demo_identity_matches(monkeypatch):
    _demo(monkeypatch, True)
    assert is_demo_email(DEMO_EMAIL) is True
    assert is_demo_email(f"  {DEMO_EMAIL.upper()} ") is True
    assert is_demo_email(f"x{DEMO_EMAIL}") is False
    assert is_demo_csm_id("sarah_c") is True
    assert is_demo_csm_id("user_abc") is False


def test_demo_off_provisioning_treats_demo_address_as_normal_user(monkeypatch):
    _demo(monkeypatch, False)
    from unittest.mock import patch
    from product.subscription import provision_customer_on_first_auth
    from product.database import get_or_create_customer
    with patch("product.auth_clerk.fetch_clerk_user_profile"):
        result = provision_customer_on_first_auth("user_demo_sub", DEMO_EMAIL, "Sarah")
    assert result is not None and result["subscription_status"] == "trialing"
    assert _row("user_demo_sub").subscription_status == "trialing"
    _s = _TestSession()
    _s.query(Customer).delete(); _s.commit(); _s.close()
    with patch("product.auth_clerk.fetch_clerk_user_profile"):
        get_or_create_customer("user_demo_goc", DEMO_EMAIL, "Sarah")
    goc = _row("user_demo_goc")
    assert goc.subscription_status == "trialing" and goc.trial_ends_at is not None


def test_demo_on_provisioning_bypasses_demo_address(monkeypatch):
    _demo(monkeypatch, True)
    from unittest.mock import patch
    from product.subscription import provision_customer_on_first_auth
    from product.database import get_or_create_customer
    with patch("product.auth_clerk.fetch_clerk_user_profile"):
        assert provision_customer_on_first_auth("user_demo_sub", DEMO_EMAIL, "Sarah") is None
        get_or_create_customer("user_demo_goc", DEMO_EMAIL, "Sarah")
    assert _row("user_demo_sub") is None
    goc = _row("user_demo_goc")
    assert goc.subscription_status == "none" and goc.trial_ends_at is None


DEMO_ROUTE_SAMPLES = [
    ("get", "/demo"),
    ("get", "/priorities/midday"),
    ("get", "/northgate/alert"),
    ("get", "/assistant/today"),
    ("get", "/login"),
    ("get", "/app/session"),
]


@pytest.mark.parametrize("method,path", DEMO_ROUTE_SAMPLES)
def test_demo_off_demo_routes_404(monkeypatch, method, path):
    _demo(monkeypatch, False)
    client = TestClient(main.app)
    r = getattr(client, method)(path, follow_redirects=False)
    assert r.status_code == 404


def test_demo_on_demo_routes_served(monkeypatch):
    _demo(monkeypatch, True)
    monkeypatch.setattr(main, "DEMO_PASSWORD", "test-demo-password")
    client = TestClient(main.app)
    assert client.get("/priorities/midday").status_code == 200
    assert client.get("/northgate/alert").status_code == 200
    assert client.get("/demo").status_code == 200


def test_non_demo_routes_unaffected_by_demo_off(monkeypatch):
    _demo(monkeypatch, False)
    client = TestClient(main.app)
    assert client.get("/health").status_code == 200


def test_demo_off_unauthenticated_chat_is_401(monkeypatch):
    _demo(monkeypatch, False)
    monkeypatch.setattr(main, "_resolve_auth", AsyncMock(return_value=None))
    client = TestClient(main.app)
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 401


def test_demo_off_legacy_session_cookie_ignored(monkeypatch):
    from product.auth import create_session
    from starlette.requests import Request
    monkeypatch.setattr(main, "verify_clerk_token", AsyncMock(return_value=None))
    token = create_session("sarah_c", "Sarah", DEMO_EMAIL)
    scope = {"type": "http", "headers": [(b"cookie", f"{main.SESSION_COOKIE}={token}".encode())]}
    import asyncio
    _demo(monkeypatch, False)
    assert asyncio.run(main._resolve_auth(Request(scope))) is None
    _demo(monkeypatch, True)
    session = asyncio.run(main._resolve_auth(Request(scope)))
    assert session and session["csm_id"] == "sarah_c"


def test_demo_off_csm_api_key_refused(monkeypatch):
    monkeypatch.setattr(main, "CSM_REGISTRY", {"configured-key": "sarah_c"})
    req = MagicMock()
    req.headers = {"X-CSM-API-Key": "configured-key"}
    _demo(monkeypatch, False)
    assert main.resolve_user_id(req) is None
    _demo(monkeypatch, True)
    assert main.resolve_user_id(req) == "sarah_c"


def test_demo_off_sarah_c_session_is_billing_gated_like_anyone(monkeypatch):
    """With DEMO_MODE off, the sarah_c id gets no billing bypass."""
    _demo(monkeypatch, False)
    _billing(monkeypatch, True)
    monkeypatch.setattr(main, "_resolve_auth", AsyncMock(return_value={"csm_id": "sarah_c", "name": "", "email": ""}))
    import asyncio
    with pytest.raises(main.SubscriptionGatedException):
        asyncio.run(main._require_active_subscription(MagicMock()))


# ── BILLING_ENABLED ──────────────────────────────────────────────────────────

def _session_for(monkeypatch, csm_id: str):
    monkeypatch.setattr(main, "_resolve_auth", AsyncMock(return_value={"csm_id": csm_id, "name": "", "email": ""}))


def test_billing_off_no_subscription_lockout(monkeypatch, no_stripe):
    import asyncio
    _billing(monkeypatch, False)
    _demo(monkeypatch, False)
    _add_customer("user_canceled", "canceled")
    _add_customer("user_expired", "trialing", datetime.now(timezone.utc) - timedelta(days=1))
    for cid in ("user_canceled", "user_expired", "user_no_row"):
        _session_for(monkeypatch, cid)
        session = asyncio.run(main._require_active_subscription(MagicMock()))
        assert session["csm_id"] == cid
    client = TestClient(main.app)
    _session_for(monkeypatch, "user_canceled")
    body = client.get("/app/subscription-status").json()
    assert body["is_gated"] is False
    assert body["has_stripe_customer"] is False
    _assert_no_stripe(no_stripe)


def test_billing_off_no_stripe_calls_on_billing_routes(monkeypatch, no_stripe):
    _billing(monkeypatch, False)
    _session_for(monkeypatch, "user_any")
    client = TestClient(main.app)
    assert client.post("/app/billing/checkout-session").status_code == 404
    assert client.post("/app/billing/portal-session").status_code == 404
    for path in ("/billing/webhook", "/webhooks/stripe"):
        r = client.post(path, content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
        assert r.status_code == 404
    _assert_no_stripe(no_stripe)


def test_billing_off_status_page_skips_stripe(monkeypatch, no_stripe):
    _billing(monkeypatch, False)
    import product.billing as _b

    class _Resp:
        status_code = 401

    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(_b, "STRIPE_SECRET_KEY", "stripe-secret-key-placeholder")
    names = [c["name"] for c in main._run_status_checks()]
    assert "Stripe" not in names
    _assert_no_stripe(no_stripe)


def test_billing_off_billing_helpers_refuse_without_calling_stripe(monkeypatch, no_stripe):
    _billing(monkeypatch, False)
    import product.billing as _b
    monkeypatch.setattr(_b, "STRIPE_SECRET_KEY", "stripe-secret-key-placeholder")
    with pytest.raises(EnvironmentError):
        _b.get_subscription_status("a@example.com")
    with pytest.raises(EnvironmentError):
        _b.create_customer_portal_session("a@example.com")
    _assert_no_stripe(no_stripe)


def test_billing_off_trial_jobs_skipped(monkeypatch):
    _billing(monkeypatch, False)
    import product.subscription as _sub
    expire = MagicMock()
    monkeypatch.setattr(_sub, "expire_trials", expire)
    main._expire_trials()
    assert expire.call_count == 0
    sess = MagicMock()
    monkeypatch.setattr(_db, "SessionLocal", sess)
    main._process_trial_emails()
    assert sess.call_count == 0


def test_billing_on_gate_unchanged(monkeypatch):
    import asyncio
    _billing(monkeypatch, True)
    _demo(monkeypatch, False)
    _add_customer("user_canceled", "canceled")
    _add_customer("user_trial", "trialing", datetime.now(timezone.utc) + timedelta(days=3))
    _session_for(monkeypatch, "user_canceled")
    with pytest.raises(main.SubscriptionGatedException):
        asyncio.run(main._require_active_subscription(MagicMock()))
    _session_for(monkeypatch, "user_no_row")
    with pytest.raises(main.SubscriptionGatedException):
        asyncio.run(main._require_active_subscription(MagicMock()))
    _session_for(monkeypatch, "user_trial")
    assert asyncio.run(main._require_active_subscription(MagicMock()))["csm_id"] == "user_trial"
    client = TestClient(main.app)
    _session_for(monkeypatch, "user_canceled")
    assert client.get("/app/subscription-status").json()["is_gated"] is True


def test_billing_on_fails_closed_on_db_error(monkeypatch):
    import asyncio
    _billing(monkeypatch, True)
    _demo(monkeypatch, False)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(_db, "SessionLocal", _boom)
    _session_for(monkeypatch, "user_any")
    with pytest.raises(main.SubscriptionGatedException):
        asyncio.run(main._require_active_subscription(MagicMock()))


def test_billing_on_webhook_without_secret_still_rejected(monkeypatch, no_stripe):
    _billing(monkeypatch, True)
    import product.billing as _b
    monkeypatch.setattr(_b, "STRIPE_WEBHOOK_SECRET", "")
    client = TestClient(main.app)
    r = client.post("/billing/webhook", content=b"{}", headers={"stripe-signature": "x"})
    assert r.status_code >= 400
    _assert_no_stripe(no_stripe)


def test_demo_on_billing_off_demo_still_served(monkeypatch):
    _demo(monkeypatch, True)
    _billing(monkeypatch, False)
    client = TestClient(main.app)
    assert client.get("/priorities/midday").status_code == 200


# ── Hardcoded production URL / model guard ───────────────────────────────────

_PROD_URL = re.compile(r"https?://(www\.)?cs-agent\.app\b")
_MODEL_ID = re.compile(r"claude-(sonnet|opus|haiku|fable|instant|\d)[a-z0-9.\-]*")

# Files allowed to contain the production URL:
#   - product/settings.py (the one place settings live; it holds no prod URL today)
#   - privacy/terms pages: legal documents, out of scope for this change
#   (security.txt now uses {{PUBLIC_BASE_URL}} placeholders filled at serve time)
_URL_EXEMPT = {
    "product/settings.py",
    "product/static/privacy.html",
    "product/static/terms.html",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    keep = []
    for f in out:
        if f.startswith(("tests/", "frontend/tests/", "docs/")) or f.endswith(".md"):
            continue
        if f in ("ui_prototype_2.html", "CODE_REVIEW.html", ".env.example"):
            continue
        if not f.endswith((".py", ".js", ".jsx", ".html", ".txt", ".sh", ".yml", ".json")):
            continue
        if f.endswith("package-lock.json"):
            continue
        keep.append(f)
    return keep


def test_no_hardcoded_production_url_outside_settings():
    offenders = []
    for f in _tracked_files():
        if f in _URL_EXEMPT:
            continue
        p = REPO_ROOT / f
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if _PROD_URL.search(line):
                offenders.append(f"{f}:{i}")
    assert not offenders, "hardcoded production URL; use product.settings.public_url():\n" + "\n".join(offenders)


def test_no_hardcoded_model_id_outside_settings():
    offenders = []
    for f in _tracked_files():
        if f == "product/settings.py" or not f.endswith(".py"):
            continue
        p = REPO_ROOT / f
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if _MODEL_ID.search(line):
                offenders.append(f"{f}:{i}: {line.strip()[:80]}")
    assert not offenders, "hardcoded model id; use product.settings.ANTHROPIC_MODEL:\n" + "\n".join(offenders)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ── Personal-details guard ───────────────────────────────────────────────────
# Fails if the operator's personal domain, the operator's contact addresses,
# or the two production Clerk user ids appear in product code. Values are
# compared by SHA-256, loaded from scripts/public_copy_markers.json (shared
# with scripts/build_public_copy.py). That file is excluded from the public
# copy, because hashes of short values can be guessed; without it these
# scans skip.

import hashlib  # noqa: E402
import json  # noqa: E402

_MARKERS_PATH = REPO_ROOT / "scripts" / "public_copy_markers.json"
_MARKERS = json.loads(_MARKERS_PATH.read_text()) if _MARKERS_PATH.exists() else None

_BLOCKED_DOMAIN_ANYWHERE = set(_MARKERS["domains_anywhere"]) if _MARKERS else set()
_BLOCKED_EMAIL_DOMAIN = set(_MARKERS["email_domains"]) if _MARKERS else set()
_BLOCKED_CLERK_IDS = set(_MARKERS["clerk_ids"]) if _MARKERS else set()

_needs_markers = pytest.mark.skipif(
    _MARKERS is None, reason="private markers file not present (public copy)"
)

# Files that still carry these values on purpose:
#   - privacy.html / terms.html: legal documents, not edited in code changes
#   - org_config.json / pilot_config.json: the live deployment's config.
#     product/org_config.py loads them from the repo root at runtime, so
#     production still needs them. Tests read the *.example.json copies
#     (tests/conftest.py), which are NOT exempt.
_PERSONAL_EXEMPT = {
    "product/static/privacy.html",
    "product/static/terms.html",
    "org_config.json",
    "pilot_config.json",
}


@_needs_markers
def test_example_configs_are_scanned_and_neutral():
    assert "org_config.example.json" not in _PERSONAL_EXEMPT
    assert "pilot_config.example.json" not in _PERSONAL_EXEMPT
    for name in ("org_config.example.json", "pilot_config.example.json"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert _personal_hits(text) == [], name

_DOMAIN_TOKEN = re.compile(r"(?<![A-Za-z0-9-])((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
_EMAIL_TOKEN = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
_CLERK_TOKEN = re.compile(r"user_[A-Za-z0-9]{27}")


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _personal_hits(text: str) -> list[str]:
    hits = []
    for m in _DOMAIN_TOKEN.finditer(text):
        d = m.group(1).lower()
        # also check each parent domain (sub.example.com -> example.com)
        parts = d.split(".")
        for i in range(len(parts) - 1):
            if _h(".".join(parts[i:])) in _BLOCKED_DOMAIN_ANYWHERE:
                hits.append("personal domain")
    for m in _EMAIL_TOKEN.finditer(text):
        if _h(m.group(1).lower()) in _BLOCKED_EMAIL_DOMAIN:
            hits.append("operator contact address")
    for m in _CLERK_TOKEN.finditer(text):
        if _h(m.group(0)) in _BLOCKED_CLERK_IDS:
            hits.append("production Clerk id")
    return hits


def test_personal_guard_detects_values(monkeypatch):
    """Self-check with stand-in values: the guard fires on each shape it is
    meant to catch, and stays quiet on neutral ones."""
    assert _personal_hits("nothing here: demo@example.com user_" + "a" * 27) == []
    fake_id = "user_" + "Z" * 27
    monkeypatch.setattr(sys.modules[__name__], "_BLOCKED_DOMAIN_ANYWHERE", {_h("blocked.test")})
    monkeypatch.setattr(sys.modules[__name__], "_BLOCKED_EMAIL_DOMAIN", {_h("contact.test")})
    monkeypatch.setattr(sys.modules[__name__], "_BLOCKED_CLERK_IDS", {_h(fake_id)})
    assert _personal_hits("see https://www.blocked.test/x") == ["personal domain"]
    assert _personal_hits("a@BLOCKED.test") == ["personal domain"]
    assert _personal_hits("mail hello@contact.test") == ["operator contact address"]
    assert _personal_hits("link https://contact.test only") == []
    assert _personal_hits(f'id = "{fake_id}"') == ["production Clerk id"]


@_needs_markers
def test_markers_loaded():
    assert len(_BLOCKED_CLERK_IDS) == 2
    assert _BLOCKED_DOMAIN_ANYWHERE and _BLOCKED_EMAIL_DOMAIN


@_needs_markers
def test_no_personal_details_in_product_code():
    offenders = []
    for f in _tracked_files():
        if f in _PERSONAL_EXEMPT:
            continue
        p = REPO_ROOT / f
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            for kind in _personal_hits(line):
                offenders.append(f"{f}:{i}: {kind}")
    assert not offenders, "personal details in product code; use product.settings:\n" + "\n".join(offenders)
