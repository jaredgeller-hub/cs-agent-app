"""
tests/test_templated_pages.py

The security policies (docs/policies/*.md) and the accessibility, docs and
support pages carry {{COMPANY_NAME}}, {{COMPANY_LEGAL_NAME}} and
{{COMPANY_DOMAIN}} placeholders, filled from settings when served.

1. With the production company settings, every page renders byte-for-byte
   identical to its output before the placeholders were introduced
   (source at BASELINE_COMMIT, the last commit before the change).
2. With the default settings, no page ever shows a raw {{...}} token; the
   company name renders as the default "Your Company", including on the raw
   /static/*.html paths.

The production values are assembled at runtime so this file, which ships
in the public copy, does not spell the company name.

Standalone-runnable:
    python3 tests/test_templated_pages.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient

import main  # noqa: E402
import product.settings as settings  # noqa: E402

BASELINE_COMMIT = "67ec6a4af8932a6957ab3946d98a02cde06e8328"

_NAME = "Al" + "waison"
PROD = {
    "COMPANY_NAME": _NAME,
    "COMPANY_LEGAL_NAME": _NAME + ", Inc.",
    "COMPANY_DOMAIN": _NAME.lower() + ".com",
    "SUPPORT_EMAIL": "hello@" + _NAME.lower() + ".com",
    "PUBLIC_BASE_URL": "https://cs-agent.app",
}

STATIC_PAGES = ["accessibility.html", "docs.html", "support.html"]
POLICY_FILES = {
    slug: filename for slug, (filename, _title, _desc) in main._SECURITY_POLICIES.items()
}


def _baseline(path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:{path}"], cwd=REPO_ROOT,
        capture_output=True, text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


_has_baseline = _baseline("product/static/support.html") is not None
needs_baseline = pytest.mark.skipif(
    not _has_baseline, reason="baseline commit not in this repo's history (e.g. public copy)"
)


@pytest.fixture
def prod_settings(monkeypatch):
    for k, v in PROD.items():
        monkeypatch.setattr(main, k, v)


@pytest.fixture
def default_settings(monkeypatch):
    monkeypatch.setattr(main, "COMPANY_NAME", settings.DEFAULT_COMPANY_NAME)
    monkeypatch.setattr(main, "COMPANY_LEGAL_NAME", settings.DEFAULT_COMPANY_NAME)
    monkeypatch.setattr(main, "COMPANY_DOMAIN", settings.DEFAULT_COMPANY_DOMAIN)
    monkeypatch.setattr(main, "SUPPORT_EMAIL", settings.DEFAULT_SUPPORT_EMAIL)


# ── 1. Identical render with production settings ─────────────────────────────

@needs_baseline
@pytest.mark.parametrize("name", STATIC_PAGES)
def test_static_page_renders_identically_to_baseline(prod_settings, name):
    old = main._fill_page_tokens(_baseline(f"product/static/{name}"))
    new = main._fill_page_tokens((REPO_ROOT / "product" / "static" / name).read_text(encoding="utf-8"))
    assert new == old
    served = TestClient(main.app).get("/" + name.removesuffix(".html"))
    assert served.status_code == 200
    assert served.text == old


@needs_baseline
@pytest.mark.parametrize("slug", sorted(POLICY_FILES))
def test_policy_renders_identically_to_baseline(prod_settings, slug):
    filename = POLICY_FILES[slug]
    title = main._SECURITY_POLICIES[slug][1]
    old = main._render_policy_markdown(_baseline(f"docs/policies/{filename}"), title)
    new = main._render_security_policies()[slug]
    assert new == old


@needs_baseline
def test_baseline_sources_had_no_company_tokens():
    """The comparison is meaningful only if the baseline was literal text."""
    for path in [f"product/static/{n}" for n in STATIC_PAGES] + [
        f"docs/policies/{f}" for f in POLICY_FILES.values()
    ]:
        src = _baseline(path)
        assert "{{COMPANY_" not in src, path
        assert _NAME in src, path


def test_sources_use_placeholders_not_the_name():
    for path in [REPO_ROOT / "product" / "static" / n for n in STATIC_PAGES] + [
        REPO_ROOT / "docs" / "policies" / f for f in POLICY_FILES.values()
    ]:
        text = path.read_text(encoding="utf-8")
        assert "{{COMPANY_NAME}}" in text, path.name
        assert _NAME.lower() not in text.lower(), path.name


# SHA-256 of each page as served BEFORE the placeholders were introduced,
# captured from the running app with the production settings in PROD. The
# render below runs in a fresh process with those settings in the
# environment (some page parts are built at import), so it needs no git
# history and runs in CI.
PINNED_PROD_RENDER_SHA256 = {
    "/security": "98f44a7c750f267b5d14dae317d380143f3e8fdc63677299ef2995bd3fe334b9",
    "/security/information-security": "a4f37a4bd327c3693a5f5c9f9897b61adb0cf9d7f94523835eea6dc36ce6296e",
    "/security/incident-response": "f29e4377a7a58ede17ccc00d0265a222b584bebb8af5825014927a6594f0b41f",
    "/security/vulnerability-disclosure": "868dbecebe09344a09a6ebd896b51463ead3d276a2b97cfc9621a47478928be0",
    "/accessibility": "bfdff5c532c795ccf4fcb6ae1567069e52d783136804beef20cb21e7f00197ef",
    "/docs": "c543e3fa61d7e49e263f2e4682c6eba3207e86d6560e4ca74633c05c4acf0fe0",
    "/support": "00c453d345fb8bab8a6710d6ba3daff02995ae27844e5c49f0ef225048cbb531",
}

_RENDER_SCRIPT = """
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
import main
from fastapi.testclient import TestClient
c = TestClient(main.app)
out = {}
for path in json.loads(sys.argv[2]):
    r = c.get(path)
    out[path] = [r.status_code, hashlib.sha256(r.content).hexdigest()]
print("RESULT " + json.dumps(out))
"""


def test_served_pages_match_pre_change_render_with_prod_settings():
    import json
    import os
    env = dict(os.environ)
    env.update(PROD)
    for k in ("DATABASE_URL", "REDIS_URL"):
        env.pop(k, None)
    proc = subprocess.run(
        [sys.executable, "-c", _RENDER_SCRIPT, str(REPO_ROOT), json.dumps(sorted(PINNED_PROD_RENDER_SHA256))],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=25,
    )
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, f"render subprocess failed: {proc.stderr[-500:]}"
    got = json.loads(line[len("RESULT "):])
    for path, expected in PINNED_PROD_RENDER_SHA256.items():
        status, digest = got[path]
        assert status == 200, path
        assert digest == expected, f"{path} no longer renders byte-for-byte as before"


# ── 2. Never a raw placeholder; default is "Your Company" ────────────────────

def test_default_company_name_is_your_company():
    assert settings.DEFAULT_COMPANY_NAME == "Your Company"
    assert settings.DEFAULT_COMPANY_DOMAIN == "example.com"


@pytest.mark.parametrize("path", [
    "/accessibility", "/docs", "/support",
    "/static/accessibility.html", "/static/docs.html", "/static/support.html",
])
def test_static_pages_with_defaults_show_your_company(default_settings, path):
    r = TestClient(main.app).get(path)
    assert r.status_code == 200
    assert "{{" not in r.text
    assert "Your Company" in r.text
    assert _NAME.lower() not in r.text.lower()


def test_policies_with_defaults_show_your_company(default_settings):
    for slug, html in main._render_security_policies().items():
        assert html is not None, slug
        assert "{{" not in html, slug
        assert "Your Company" in html, slug
        assert _NAME.lower() not in html.lower(), slug


def test_every_page_token_is_filled():
    """No placeholder used in a templated source lacks a value."""
    import re
    known = set(main._page_tokens())
    for path in [REPO_ROOT / "product" / "static" / n for n in STATIC_PAGES] + [
        REPO_ROOT / "docs" / "policies" / f for f in POLICY_FILES.values()
    ] + [REPO_ROOT / "index.html"]:
        used = set(re.findall(r"\{\{[A-Z_]+\}\}", path.read_text(encoding="utf-8")))
        assert used <= known, (path.name, used - known)
    assert all(v for v in main._page_tokens().values())


def test_static_paths_served_filled_with_prod_settings(prod_settings):
    c = TestClient(main.app)
    for name in STATIC_PAGES:
        a = c.get("/" + name.removesuffix(".html")).text
        b = c.get(f"/static/{name}").text
        assert a == b, name


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
