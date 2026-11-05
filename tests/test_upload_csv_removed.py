"""
tests/test_upload_csv_removed.py

Regression guard for the removal of the demo-era POST /upload/csv endpoint.

The endpoint accepted an unauthenticated UploadFile with no antivirus
scanning and wrote decoded bytes to disk. It was removed to give a clean
"No untrusted file upload" answer on CASA Tier 2 SAQ Q9/Q10. This test
ensures the route does not silently come back: an unauthenticated POST
must return 404 (route absent), never 200/4xx-from-handler.

Standalone-runnable:
    python3 tests/test_upload_csv_removed.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def test_upload_csv_endpoint_removed():
    """POST /upload/csv must be gone (404), not handled by any route."""
    resp = client.post("/upload/csv", files={"file": ("u.csv", b"a,b\n1,2\n", "text/csv")})
    assert resp.status_code == 404, (
        f"/upload/csv should be removed but returned {resp.status_code}; "
        "the demo-era upload endpoint must not be reintroduced (CASA Q9/Q10)"
    )


if __name__ == "__main__":
    test_upload_csv_endpoint_removed()
    print("ok: /upload/csv returns 404")
