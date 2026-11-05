"""
tests/test_correlation_id.py

Regression guard for the X-Request-Id correlation contract used by the
frontend chat error path. Two invariants:

  1. When the client sends X-Request-Id, the server uses that value
     and echoes it back in the response headers.
  2. When the client does not send X-Request-Id, the server generates
     one (uuid4-derived) and echoes it back so the frontend can still
     surface a correlatable id in error messages.

Both invariants are enforced by add_correlation_id middleware in
main.py. The frontend api.js generates crypto.randomUUID and sends it;
ad-hoc clients (curl, scripts) get a server-generated id.

Standalone-runnable:
    python3 tests/test_correlation_id.py
"""

import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("STRIPE_PRICE_ID", "price_test_fixture")

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def test_client_supplied_request_id_is_echoed():
    """The frontend sends crypto.randomUUID() as X-Request-Id. The
    middleware must use that exact value (no truncation, no rewrite)
    and echo it on the response so the browser can correlate.
    """
    rid = str(uuid.uuid4())
    # Use any unauthenticated route; the middleware runs regardless of
    # the route handler. /app/subscription-status returns 401 without
    # auth, which is fine: we only care about the response header.
    resp = client.get("/app/subscription-status", headers={"X-Request-Id": rid})
    assert resp.headers.get("x-request-id") == rid, (
        f"client-supplied request id not echoed: "
        f"sent={rid!r} got={resp.headers.get('x-request-id')!r}"
    )
    print("OK client-supplied X-Request-Id is echoed verbatim")


def test_server_generates_request_id_when_absent():
    """Ad-hoc clients (curl, scripts) do not send X-Request-Id. The
    middleware must generate one and echo it so logs and responses
    still correlate.
    """
    resp = client.get("/app/subscription-status")
    rid = resp.headers.get("x-request-id")
    assert rid, f"server did not generate X-Request-Id: headers={dict(resp.headers)!r}"
    assert isinstance(rid, str) and len(rid) >= 4, (
        f"generated X-Request-Id looks malformed: {rid!r}"
    )
    print(f"OK server-generated X-Request-Id present (got {rid!r})")


def test_request_id_differs_across_requests():
    """Sanity check on uniqueness when the client does not supply an
    id: two consecutive requests get different server-generated ids.
    """
    r1 = client.get("/app/subscription-status")
    r2 = client.get("/app/subscription-status")
    rid1 = r1.headers.get("x-request-id")
    rid2 = r2.headers.get("x-request-id")
    assert rid1 and rid2, "missing X-Request-Id on at least one response"
    assert rid1 != rid2, (
        f"server-generated request ids collided across two requests: "
        f"both were {rid1!r}"
    )
    print("OK two server-generated X-Request-Ids differ")


def main_runner():
    tests = [
        test_client_supplied_request_id_is_echoed,
        test_server_generates_request_id_when_absent,
        test_request_id_differs_across_requests,
    ]
    failures = []
    for t in tests:
        try:
            t()
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
