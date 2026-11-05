"""
tests/test_security_headers.py

Regression guard for the HSTS (Strict-Transport-Security) header set by
add_security_headers middleware in main.py. The header must appear on
every response, including error responses and unauthenticated routes,
so SSL Labs and CASA Tier 2 scans see it consistently.

Standalone-runnable:
    python3 tests/test_security_headers.py
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("STRIPE_PRICE_ID", "price_test_fixture")

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)

HSTS_VALUE = "max-age=31536000; includeSubDomains; preload"


def test_hsts_header_present_on_root():
    """HSTS header is set on all responses."""
    response = client.get("/")
    assert response.headers.get("strict-transport-security") == HSTS_VALUE


def test_hsts_header_present_on_health():
    """HSTS header is set on health endpoint responses."""
    response = client.get("/pilot/health")
    assert response.headers.get("strict-transport-security") == HSTS_VALUE


def test_hsts_header_present_on_404():
    """HSTS header is set even on 404 responses."""
    response = client.get("/this-route-does-not-exist-12345")
    assert response.headers.get("strict-transport-security") == HSTS_VALUE


def main_runner():
    tests = [
        test_hsts_header_present_on_root,
        test_hsts_header_present_on_health,
        test_hsts_header_present_on_404,
    ]
    failures = []
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__}")
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failures.append(t.__name__)
        except Exception as e:
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failures.append(t.__name__)
    print()
    if failures:
        print(f"FAILED: {len(failures)} of {len(tests)}")
        for n in failures:
            print(f"  - {n}")
        sys.exit(1)
    print(f"PASSED: {len(tests)} of {len(tests)}")


if __name__ == "__main__":
    main_runner()
