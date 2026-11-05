"""
tests/test_context_cache_summary.py

Tests for product.context_cache._build_summary_text. This pure function
generates the chat system-prompt summary from CRM snapshot data plus a
per-account source_status map. It is the surface where SourceStatus from
the cache write path becomes user-facing text the LLM reads.

Coverage:
  - account_status=None preserves pre-commit-2 output (legacy / Gainsight).
  - account_status with all "ok" sections matches the no-status baseline.
  - account_status with "failed" sections appends a "Data fetch issues" line.
  - "empty" status is treated as a clean empty (no marker).
  - Mixed statuses across accounts are rendered correctly.

Standalone-runnable:
    python3 tests/test_context_cache_summary.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from product.context_cache import _build_summary_text


def _baseline_inputs():
    return dict(
        all_accounts=[
            {"hubspot_id": 100, "name": "Meridian Health"},
            {"hubspot_id": 200, "name": "Beacon Financial"},
            {"hubspot_id": 300, "name": "Sutton Retail"},
        ],
        deals=[
            {"account_name": "Meridian Health", "name": "Renewal", "stage": "open",
             "amount": 50000, "close_date": "2026-08-01", "days_until_close": 90}
        ],
        contacts=[
            {"account_name": "Meridian Health", "name": "Marcus Reyes", "role": "VP"}
        ],
        tickets=[],
        health_scores=[],
    )


def test_build_summary_text_no_account_status_matches_legacy_output():
    """account_status=None reproduces pre-commit-2 string exactly."""
    args = _baseline_inputs()

    text_legacy = _build_summary_text(account_status=None, **args)
    text_empty_dict = _build_summary_text(account_status={}, **args)

    assert "Data fetch issues" not in text_legacy
    assert text_legacy == text_empty_dict
    assert "User manages 3 accounts." in text_legacy
    assert "Meridian Health (contact: Marcus Reyes (VP))" in text_legacy


def test_build_summary_text_all_ok_no_marker():
    """When all per-account sections are ok or empty, no marker line is added."""
    args = _baseline_inputs()
    account_status = {
        "100": {"contacts": "ok", "deals": "ok", "tickets": "empty"},
        "200": {"contacts": "empty", "deals": "empty", "tickets": "empty"},
        "300": {"contacts": "ok", "deals": "empty", "tickets": "ok"},
    }

    text = _build_summary_text(account_status=account_status, **args)

    assert "Data fetch issues" not in text


def test_build_summary_text_failed_section_emits_marker():
    """A 'failed' status produces a 'Data fetch issues' line naming account+section."""
    args = _baseline_inputs()
    account_status = {
        "100": {"contacts": "failed", "deals": "ok", "tickets": "empty"},
        "200": {"contacts": "ok", "deals": "ok", "tickets": "empty"},
    }

    text = _build_summary_text(account_status=account_status, **args)

    assert "Data fetch issues:" in text
    assert "Meridian Health contacts unavailable (fetch failed)" in text
    assert "Beacon Financial" not in text.split("Data fetch issues:")[1]


def test_build_summary_text_distinguishes_failed_from_empty():
    """Builder output is distinct between failed and empty for the same account.

    This is the regression test for F11: a fetch that returned zero records
    must read differently than a fetch that errored.
    """
    args = _baseline_inputs()

    text_empty = _build_summary_text(
        account_status={"100": {"contacts": "empty"}},
        **args,
    )
    text_failed = _build_summary_text(
        account_status={"100": {"contacts": "failed"}},
        **args,
    )

    assert text_empty != text_failed
    assert "Data fetch issues" not in text_empty
    assert "Meridian Health contacts unavailable (fetch failed)" in text_failed


def test_build_summary_text_multiple_failures_listed():
    """Multiple failed sections across accounts all appear in the marker line."""
    args = _baseline_inputs()
    account_status = {
        "100": {"contacts": "failed", "deals": "failed", "tickets": "ok"},
        "200": {"contacts": "ok", "deals": "ok", "tickets": "failed"},
    }

    text = _build_summary_text(account_status=account_status, **args)

    assert "Meridian Health contacts unavailable (fetch failed)" in text
    assert "Meridian Health deals unavailable (fetch failed)" in text
    assert "Beacon Financial tickets unavailable (fetch failed)" in text


def test_build_summary_text_unknown_hubspot_id_skipped():
    """A hubspot_id in account_status that isn't in all_accounts is silently skipped."""
    args = _baseline_inputs()
    account_status = {
        "999999": {"contacts": "failed"},
    }

    text = _build_summary_text(account_status=account_status, **args)

    assert "Data fetch issues" not in text


def main_runner():
    """Standalone runner."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
