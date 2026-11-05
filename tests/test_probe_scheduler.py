"""
tests/test_probe_scheduler.py

Covers product.worker.job_probe_all_connections — the eager
connection-state probe that fans out across every user with at least
one connections row.

Verifies:
- Iterates every CSM returned by list_all_csms_with_connections and
  calls write_connection_state per CSM
- Per-CSM failures are logged WARNING and the loop continues (one bad
  token must not starve the rest of the fan-out)
- Emits the summary log with correct counts
  (event=probe_all_connections_complete csms_processed=N csms_failed=M)
- Returns a string summary
- Empty eligibility list short-circuits cleanly

No APScheduler is actually scheduled here. The 1-hour IntervalTrigger
registration in main.py:_start_scheduler is exercised only via import +
ast parse + smoke; an end-to-end "fire after an hour" test would be too
slow and too coupled to the scheduler clock.

Standalone-runnable: python3 tests/test_probe_scheduler.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.worker import job_probe_all_connections


def _run(coro):
    return asyncio.run(coro)


def test_job_iterates_every_eligible_csm():
    csm_ids = ["user_a", "user_b", "user_c"]
    seen = []

    async def fake_write(csm_id):
        seen.append(csm_id)
        return {}

    with patch("product.org_config.list_all_csms_with_connections",
               return_value=csm_ids), \
         patch("product.connection_writer.write_connection_state",
               new=AsyncMock(side_effect=fake_write)):
        result = _run(job_probe_all_connections({"job_try": 1}))

    assert sorted(seen) == sorted(csm_ids)
    assert "processed=3" in result
    assert "failed=0" in result


def test_job_continues_after_per_csm_failure(caplog):
    """A write failure for one CSM must not abort the whole fan-out."""
    csm_ids = ["user_ok1", "user_fails", "user_ok2"]

    async def flaky(csm_id):
        if csm_id == "user_fails":
            raise RuntimeError("token store unavailable")
        return {}

    caplog.set_level(logging.WARNING, logger="cs-agent.worker")
    with patch("product.org_config.list_all_csms_with_connections",
               return_value=csm_ids), \
         patch("product.connection_writer.write_connection_state",
               new=AsyncMock(side_effect=flaky)):
        result = _run(job_probe_all_connections({"job_try": 1}))

    assert "processed=3" in result
    assert "failed=1" in result
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "event=probe_all_connections_csm_failed" in m and "user_fails" in m
        for m in warnings
    ), warnings


def test_job_emits_summary_log_with_counts(caplog):
    csm_ids = ["user_one", "user_two"]

    caplog.set_level(logging.INFO, logger="cs-agent.worker")
    with patch("product.org_config.list_all_csms_with_connections",
               return_value=csm_ids), \
         patch("product.connection_writer.write_connection_state",
               new=AsyncMock(return_value={})):
        _run(job_probe_all_connections({"job_try": 1}))

    msgs = [r.getMessage() for r in caplog.records]
    summary = [m for m in msgs if "event=probe_all_connections_complete" in m]
    assert len(summary) == 1
    assert "csms_processed=2" in summary[0]
    assert "csms_failed=0" in summary[0]
    assert "duration_ms=" in summary[0]


def test_job_handles_empty_eligibility(caplog):
    caplog.set_level(logging.INFO, logger="cs-agent.worker")
    with patch("product.org_config.list_all_csms_with_connections",
               return_value=[]), \
         patch("product.connection_writer.write_connection_state",
               new=AsyncMock(return_value={})) as writer_mock:
        result = _run(job_probe_all_connections({"job_try": 1}))

    writer_mock.assert_not_called()
    assert "zero eligible CSMs" in result
    msgs = [r.getMessage() for r in caplog.records]
    assert any("csms_processed=0" in m for m in msgs)


def test_apscheduler_registration_present_in_main():
    """main.py:_start_scheduler must add a job with id 'probe_all_connections_job'.
    Static check via source scan keeps the regression cheap — running the
    scheduler in-test would spawn a real background thread."""
    src = (REPO_ROOT / "main.py").read_text()
    assert "probe_all_connections_job" in src, (
        "expected APScheduler job id 'probe_all_connections_job' in main.py"
    )
    assert "send_probe_all_connections_job" in src
    assert "IntervalTrigger(hours=1)" in src


def test_startup_hook_present_in_main():
    """The probe-on-deploy hook must exist as an @app.on_event('startup')
    handler so fresh state lands immediately after every Render deploy."""
    src = (REPO_ROOT / "main.py").read_text()
    assert "_probe_all_connections_on_deploy" in src
    # Enqueues with dedup so a slow restart doesn't double-queue
    assert "probe_all_connections_startup" in src


def test_worker_registers_job_in_functions_list():
    """WorkerSettings.functions must include job_probe_all_connections or
    arq will not pick up the job when main.py enqueues it."""
    from product.worker import WorkerSettings, job_probe_all_connections
    assert job_probe_all_connections in WorkerSettings.functions


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
