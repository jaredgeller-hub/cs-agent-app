"""
tests/test_digest_admin_trigger_async.py

Regression for bug 1 (June 3 canary): the POST /admin/trigger-digest
handler (main.admin_trigger_digest) must run the SYNC builder
_run_digest_for_user OFF the event loop.

_run_digest_for_user contains three asyncio.run() calls (the Stage 4
up-front context refresh, the Drive transcript fetch, and the refresh
inside score_account_urgency). asyncio.run() raises "cannot be called
from a running event loop" if it executes on the handler's loop thread.
The handler therefore dispatches via asyncio.to_thread, which runs the
builder on a worker thread with no running loop.

This test stubs _run_digest_for_user and records whether a running event
loop is present at call time. Pre-fix (inline call) it would be True;
post-fix (asyncio.to_thread) it must be False.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient


def test_admin_trigger_digest_runs_builder_off_loop(monkeypatch):
    import main

    captured = {}

    def _stub(csm_id):
        try:
            asyncio.get_running_loop()
            captured["loop_running"] = True
        except RuntimeError:
            captured["loop_running"] = False
        captured["csm_id"] = csm_id

    monkeypatch.setattr(main, "_run_digest_for_user", _stub)
    monkeypatch.setenv("ADMIN_SECRET", "test-admin-secret")

    client = TestClient(main.app)
    resp = client.post(
        "/admin/trigger-digest",
        headers={"X-Admin-Key": "test-admin-secret"},
        json={"csm_id": "test_csm"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "complete", "csm_id": "test_csm"}
    # The builder must have executed on a worker thread with NO running loop.
    assert captured["loop_running"] is False
    assert captured["csm_id"] == "test_csm"
    print("✓ admin_trigger_digest dispatches builder off the event loop "
          "(loop_running is False)")


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v", "-s"]))
