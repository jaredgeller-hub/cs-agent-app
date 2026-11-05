"""
tests/test_accessibility_page.py

Verifies the public accessibility statement at GET /accessibility, served
from product/static/accessibility.html in the same visual shell as /privacy
and /terms.

Standalone-runnable:
    python3 tests/test_accessibility_page.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def test_accessibility_page_returns_200_with_html():
    """GET /accessibility returns the statement as HTML."""
    resp = client.get("/accessibility")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    assert resp.headers["content-type"].startswith("text/html"), (
        f"expected text/html, got {resp.headers.get('content-type')!r}"
    )
    body = resp.text
    assert "Accessibility" in body
    assert "WCAG 2.1" in body


if __name__ == "__main__":
    test_accessibility_page_returns_200_with_html()
    print("ok: /accessibility renders")
