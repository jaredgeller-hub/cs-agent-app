"""
tests/test_operator_settings.py

Operator-identity settings in product/settings.py:
DEMO_EMAIL, DEMO_CSM_ID, SUPPORT_EMAIL, FROM_EMAIL, COMPANY_NAME,
COMPANY_LEGAL_NAME, SENDER_NAME, SENDER_TITLE, WAITLIST_FORM_URL.

Covers each setting's default and override, the places that consume them
(trial email signature, security pages, status page, served static pages,
HubSpot stub message, From address), and the /demo gate failing closed when
DEMO_MODE is on and DEMO_PASSWORD is unset.

Standalone-runnable:
    python3 tests/test_operator_settings.py
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient

import product.settings as settings

import main  # noqa: E402

_ALL_VARS = (
    "DEMO_EMAIL", "DEMO_CSM_ID", "SUPPORT_EMAIL", "FROM_EMAIL", "COMPANY_NAME",
    "COMPANY_LEGAL_NAME", "SENDER_NAME", "SENDER_TITLE", "WAITLIST_FORM_URL",
)


@pytest.fixture
def fresh_settings(monkeypatch):
    """Reload product.settings under a controlled env; restore afterwards."""
    for v in _ALL_VARS:
        monkeypatch.delenv(v, raising=False)

    def _reload(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(settings)

    yield _reload
    monkeypatch.undo()
    importlib.reload(settings)


# ── Defaults and overrides ───────────────────────────────────────────────────

def test_defaults(fresh_settings):
    s = fresh_settings()
    assert s.DEMO_EMAIL == "demo@example.com"
    assert s.DEMO_CSM_ID == "demo_user"
    assert s.SUPPORT_EMAIL == "support@example.com"
    assert s.FROM_EMAIL == "noreply@example.com"
    assert s.COMPANY_NAME == "Your Company"
    assert s.COMPANY_LEGAL_NAME == "Your Company"
    assert s.SENDER_NAME == "The team"
    assert s.SENDER_TITLE == ""
    assert s.WAITLIST_FORM_URL == "mailto:support@example.com"


def test_blank_values_fall_back_to_defaults(fresh_settings):
    s = fresh_settings(**{v: "   " for v in _ALL_VARS})
    assert s.DEMO_EMAIL == "demo@example.com"
    assert s.DEMO_CSM_ID == "demo_user"
    assert s.SUPPORT_EMAIL == "support@example.com"
    assert s.FROM_EMAIL == "noreply@example.com"
    assert s.COMPANY_NAME == "Your Company"
    assert s.SENDER_NAME == "The team"
    assert s.SENDER_TITLE == ""


def test_overrides(fresh_settings):
    s = fresh_settings(
        DEMO_EMAIL="  Demo.Person@Example.ORG ",
        DEMO_CSM_ID="demo_x",
        SUPPORT_EMAIL="help@example.org",
        FROM_EMAIL="bot@example.org",
        COMPANY_NAME="Acme",
        COMPANY_LEGAL_NAME="Acme, Inc.",
        SENDER_NAME="Pat",
        SENDER_TITLE="Founder",
        WAITLIST_FORM_URL="https://forms.example.org/w",
    )
    assert s.DEMO_EMAIL == "demo.person@example.org"  # trimmed + lower-cased
    assert s.DEMO_CSM_ID == "demo_x"
    assert s.SUPPORT_EMAIL == "help@example.org"
    assert s.FROM_EMAIL == "bot@example.org"
    assert s.COMPANY_NAME == "Acme"
    assert s.COMPANY_LEGAL_NAME == "Acme, Inc."
    assert s.SENDER_NAME == "Pat"
    assert s.SENDER_TITLE == "Founder"
    assert s.WAITLIST_FORM_URL == "https://forms.example.org/w"


def test_legal_name_and_waitlist_follow_their_base_settings(fresh_settings):
    s = fresh_settings(COMPANY_NAME="Acme", SUPPORT_EMAIL="help@example.org")
    assert s.COMPANY_LEGAL_NAME == "Acme"
    assert s.WAITLIST_FORM_URL == "mailto:help@example.org"


def test_demo_identity_reads_settings(fresh_settings, monkeypatch):
    fresh_settings(DEMO_EMAIL="d@example.org", DEMO_CSM_ID="demo_x")
    import product.demo_identity as di
    di = importlib.reload(di)
    try:
        monkeypatch.setenv("DEMO_MODE", "true")
        assert di.DEMO_EMAIL == "d@example.org"
        assert di.is_demo_email(" D@Example.org ") is True
        assert di.is_demo_email("xd@example.org") is False
        assert di.is_demo_csm_id("demo_x") is True
        assert di.is_demo_csm_id("sarah_c") is False
    finally:
        monkeypatch.undo()
        importlib.reload(settings)
        importlib.reload(di)


# ── Consumers ────────────────────────────────────────────────────────────────

def test_trial_email_signature(monkeypatch):
    import product.engagement_emails as ee
    monkeypatch.setattr(ee, "SENDER_NAME", "Pat")
    monkeypatch.setattr(ee, "COMPANY_NAME", "Acme")
    monkeypatch.setattr(ee, "SENDER_TITLE", "")
    assert ee._body(1, "Ann", "http://x").endswith("\n\nPat\nAcme")
    for day in (3, 7, 13):
        assert ee._body(day, "Ann", "http://x").endswith("\n\nPat")
    monkeypatch.setattr(ee, "SENDER_TITLE", "Founder")
    assert ee._body(1, "Ann", "http://x").endswith("\n\nPat\nFounder, Acme")


def test_trial_email_default_signature_is_neutral():
    import product.engagement_emails as ee
    if settings.SENDER_NAME != "The team" or settings.COMPANY_NAME != "Your Company":
        pytest.skip("operator settings overridden in this environment")
    assert ee._body(1, "Ann", "http://x").endswith("\n\nThe team\nYour Company")


def test_digest_failure_alert_goes_to_support_email(monkeypatch):
    import product.engagement_emails as ee
    import product.digest.delivery as delivery
    sent = {}
    monkeypatch.setattr(ee, "SUPPORT_EMAIL", "help@example.org")
    monkeypatch.setattr(delivery, "send_system_email", lambda **kw: sent.update(kw) or True)
    ee.send_digest_failure_alert("user_x", "u@example.org", "boom")
    assert sent["to"] == "help@example.org"


def test_from_email_default_used_by_delivery():
    import product.digest.delivery as delivery
    assert delivery.FROM_EMAIL == settings.FROM_EMAIL


def test_hubspot_stub_message_uses_support_email():
    import product.connectors.hubspot as hs
    assert settings.SUPPORT_EMAIL in hs._NOT_IMPLEMENTED
    with pytest.raises(NotImplementedError) as exc:
        hs.HubSpotConnector("x", {}).get_accounts()
    assert settings.SUPPORT_EMAIL in str(exc.value)


def test_security_pages_use_company_and_support_settings():
    client = TestClient(main.app)
    idx = client.get("/security").text
    assert f"CS Agent by {settings.COMPANY_NAME}" in idx
    assert f"mailto:{settings.SUPPORT_EMAIL}" in idx
    for slug in ("information-security", "incident-response", "vulnerability-disclosure"):
        page = client.get(f"/security/{slug}").text
        assert "{{" not in page, f"unfilled placeholder in /security/{slug}"
        assert f"operated by {settings.COMPANY_LEGAL_NAME}." in page
    vdp = client.get("/security/vulnerability-disclosure").text
    assert settings.SUPPORT_EMAIL in vdp
    assert "Security contact" in vdp


def test_policy_markdown_has_no_owner_name_and_uses_placeholder():
    for md in (REPO_ROOT / "docs" / "policies").glob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert "**Owner:** Security contact" in text, md.name
        assert "{{SUPPORT_EMAIL}}" in text, md.name


def test_policy_page_shell_uses_company_settings(monkeypatch):
    monkeypatch.setattr(main, "COMPANY_NAME", "Acme")
    monkeypatch.setattr(main, "COMPANY_LEGAL_NAME", "Acme, Inc.")
    html = main._policy_page("T", "<p>x</p>")
    assert "<title>T · CS Agent by Acme</title>" in html
    assert "operated by Acme, Inc." in html
    assert '<p class="eyebrow">CS Agent by Acme</p>' in html


def test_status_page_contact_uses_support_email(monkeypatch):
    monkeypatch.setattr(main, "SUPPORT_EMAIL", "help@example.org")
    html = main._render_status_html([], "2026-01-01 00:00 UTC")
    assert 'Questions? Email <a href="mailto:help@example.org">help@example.org</a>' in html


@pytest.mark.parametrize("path", ["/support", "/docs", "/accessibility"])
def test_static_pages_fill_support_email(path):
    r = TestClient(main.app).get(path)
    assert r.status_code == 200
    assert "{{" not in r.text
    assert f"mailto:{settings.SUPPORT_EMAIL}" in r.text


def test_landing_page_fills_waitlist_url(monkeypatch):
    source = (REPO_ROOT / "index.html").read_text(encoding="utf-8")
    if "{{WAITLIST_FORM_URL}}" not in source:
        pytest.skip("neutral landing page (public copy) has no waitlist links")
    monkeypatch.setattr(main, "WAITLIST_FORM_URL", "https://forms.example.org/w")
    r = TestClient(main.app).get("/")
    assert r.status_code == 200
    assert "{{" not in r.text
    assert r.text.count('href="https://forms.example.org/w"') == 4


def test_fill_page_tokens_replaces_all(monkeypatch):
    monkeypatch.setattr(main, "SUPPORT_EMAIL", "help@example.org")
    monkeypatch.setattr(main, "WAITLIST_FORM_URL", "https://w.example.org")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.org")
    out = main._fill_page_tokens("{{SUPPORT_EMAIL}} {{WAITLIST_FORM_URL}} {{PUBLIC_BASE_URL}}")
    assert out == "help@example.org https://w.example.org https://app.example.org"


def test_founder_alert_defaults_to_support_email(monkeypatch):
    import product.digest.delivery as delivery
    sent = {}
    monkeypatch.delenv("FOUNDER_EMAIL", raising=False)
    monkeypatch.setattr(main, "SUPPORT_EMAIL", "help@example.org")
    monkeypatch.setattr(delivery, "send_system_email", lambda **kw: sent.update(kw) or True)
    main._send_founder_alert("job", "csm", RuntimeError("x"), "tb")
    assert sent.get("to") == "help@example.org"
    monkeypatch.setenv("FOUNDER_EMAIL", "ops@example.org")
    main._send_founder_alert("job", "csm", RuntimeError("x"), "tb")
    assert sent.get("to") == "ops@example.org"


# ── Demo password: fail closed ───────────────────────────────────────────────

def test_demo_login_fails_closed_without_password(monkeypatch, caplog):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setattr(main, "DEMO_PASSWORD", "")
    client = TestClient(main.app)
    with caplog.at_level(logging.ERROR):
        r = client.get("/demo")
        assert r.status_code == 503
        # An empty password, a guess, and the sha256("") cookie all refused.
        # Empty form value: FastAPI's Form(...) validation refuses it (422).
        r = client.post("/demo/login", data={"password": ""}, follow_redirects=False)
        assert r.status_code == 422
        assert "demo_session" not in r.cookies
        for pw in ("guess", "csagent"):
            r = client.post("/demo/login", data={"password": pw}, follow_redirects=False)
            assert r.status_code == 503
            assert "demo_session" not in r.cookies
        client.cookies.set("demo_session", main._password_token(""))
        assert client.get("/demo").status_code == 503
    assert any("DEMO_PASSWORD is not set" in rec.getMessage() for rec in caplog.records)


def test_demo_login_works_with_password(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setattr(main, "DEMO_PASSWORD", "configured-demo-pw")
    client = TestClient(main.app)
    r = client.post("/demo/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.status_code == 200 and "demo_session" not in r.cookies
    r = client.post("/demo/login", data={"password": "configured-demo-pw"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.cookies.get("demo_session") == main._password_token("configured-demo-pw")
    client.cookies.set("demo_session", r.cookies.get("demo_session"))
    assert client.get("/demo").status_code == 200


def test_demo_password_has_no_hardcoded_default():
    src = Path(main.__file__).read_text(encoding="utf-8")
    assert 'os.getenv("DEMO_PASSWORD", "' not in src
    assert "os.environ.get(\"DEMO_PASSWORD\", \"" not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
