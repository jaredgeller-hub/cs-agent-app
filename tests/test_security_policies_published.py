"""
tests/test_security_policies_published.py

Verifies the security policy pages published at /security/* for CASA SAQ
readiness, and that security.txt advertises the Vulnerability Disclosure
Policy via the RFC 9116 Policy field.

The three policies are rendered server-side from the Markdown source of
truth in docs/policies/ into the same visual shell as /privacy and /terms:
    - GET /security                          (index listing all three)
    - GET /security/information-security
    - GET /security/incident-response
    - GET /security/vulnerability-disclosure

Standalone-runnable:
    python3 tests/test_security_policies_published.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def _assert_html_ok(resp):
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    assert resp.headers["content-type"].startswith("text/html"), (
        f"expected text/html, got {resp.headers.get('content-type')!r}"
    )


def test_security_index_returns_200_with_html():
    """GET /security returns an HTML index linking to all three policies."""
    resp = client.get("/security")
    _assert_html_ok(resp)
    body = resp.text
    assert "/security/information-security" in body
    assert "/security/incident-response" in body
    assert "/security/vulnerability-disclosure" in body


def test_security_information_security_policy_renders():
    """GET /security/information-security renders the policy (incl. its tables)."""
    resp = client.get("/security/information-security")
    _assert_html_ok(resp)
    body = resp.text
    assert "<h1>Information Security Policy</h1>" in body
    # Source markdown contains GFM tables; the 'tables' extension must render them.
    assert "<table>" in body


def test_security_incident_response_policy_renders():
    """GET /security/incident-response renders the policy."""
    resp = client.get("/security/incident-response")
    _assert_html_ok(resp)
    assert "<h1>Incident Response Policy</h1>" in resp.text


def test_security_vulnerability_disclosure_policy_renders():
    """GET /security/vulnerability-disclosure renders the policy."""
    resp = client.get("/security/vulnerability-disclosure")
    _assert_html_ok(resp)
    assert "Vulnerability Disclosure Policy" in resp.text


def test_security_txt_has_policy_field():
    """security.txt (committed source that deploys) advertises the VDP via the
    RFC 9116 Policy field, pointing at /security/vulnerability-disclosure."""
    security_txt = (
        REPO_ROOT / "frontend" / "public" / ".well-known" / "security.txt"
    ).read_text(encoding="utf-8")
    # The committed source carries deployment placeholders; main.py fills
    # them from settings when serving /.well-known/security.txt.
    import main
    from product.settings import PUBLIC_BASE_URL, SUPPORT_EMAIL
    security_txt = main._fill_page_tokens(security_txt)
    assert "{{" not in security_txt, "unfilled placeholder in security.txt"
    lines = security_txt.splitlines()
    policy_lines = [ln for ln in lines if ln.startswith("Policy:")]
    assert policy_lines, "security.txt is missing a Policy field"
    assert any(
        ln.strip() == f"Policy: {PUBLIC_BASE_URL}/security/vulnerability-disclosure"
        for ln in policy_lines
    ), f"Policy field does not point at the VDP URL: {policy_lines!r}"
    assert f"Contact: mailto:{SUPPORT_EMAIL}" in lines
    assert f"Canonical: {PUBLIC_BASE_URL}/.well-known/security.txt" in lines


if __name__ == "__main__":
    test_security_index_returns_200_with_html()
    test_security_information_security_policy_renders()
    test_security_incident_response_policy_renders()
    test_security_vulnerability_disclosure_policy_renders()
    test_security_txt_has_policy_field()
    print("ok: /security/* pages render and security.txt has Policy field")
