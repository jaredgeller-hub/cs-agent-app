"""
tests/test_title_match.py

Unit tests for product/signals/_title_match.py.

Standalone-runnable:
    python3 tests/test_title_match.py
"""

import sys
import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.signals._title_match import (
    match_title_to_account,
    group_transcripts_by_account,
)


# ── match_title_to_account ───────────────────────────────────────────────────


def test_exact_match():
    assert match_title_to_account(
        "Meeting transcript - 2026-04-28 - Northgate Logistics",
        ["Northgate Logistics", "Beacon Financial"],
    ) == "Northgate Logistics"


def test_case_insensitive_match():
    assert match_title_to_account(
        "Meeting transcript - 2026-04-28 - NORTHGATE logistics",
        ["Northgate Logistics"],
    ) == "Northgate Logistics"


def test_longest_match_wins():
    assert match_title_to_account(
        "Beacon Financial sync - April",
        ["Beacon", "Beacon Financial"],
    ) == "Beacon Financial"


def test_no_match_returns_none():
    assert match_title_to_account(
        "Random Doc 2026-04-28",
        ["Northgate Logistics", "Beacon Financial"],
    ) is None


def test_empty_title_returns_none():
    assert match_title_to_account("", ["Northgate"]) is None


def test_empty_account_list_returns_none():
    assert match_title_to_account("Northgate sync", []) is None


def test_skips_empty_account_names():
    assert match_title_to_account(
        "Northgate sync",
        ["", None, "Northgate"],
    ) == "Northgate"


def test_ambiguous_equal_length_match_logs_warning(caplog):
    """Two account names of equal length both match title.
    Matcher returns the first and emits WARNING."""
    with caplog.at_level(logging.WARNING, logger="cs-agent.title_match"):
        result = match_title_to_account(
            "Acme Corp and Beta Corp joint sync",
            ["Acme Corp", "Beta Corp"],
        )
    assert result in ("Acme Corp", "Beta Corp")
    assert any("ambiguous" in r.message for r in caplog.records)


# ── group_transcripts_by_account ─────────────────────────────────────────────


def test_group_partitions_by_account():
    transcripts = [
        {"title": "Meeting - Northgate Logistics review", "file_id": "f1"},
        {"title": "Meeting - Beacon Financial sync",      "file_id": "f2"},
        {"title": "Meeting - Northgate quarterly",        "file_id": "f3"},
    ]
    by_acct = group_transcripts_by_account(
        transcripts, ["Northgate Logistics", "Beacon Financial"],
    )
    # "Northgate quarterly" matches "Northgate Logistics" only if Northgate
    # is contained in it. It is not, so f3 only matches if account list also
    # has "Northgate". We listed "Northgate Logistics" so f3 should NOT match.
    assert "Northgate Logistics" in by_acct
    assert {t["file_id"] for t in by_acct["Northgate Logistics"]} == {"f1"}
    assert "Beacon Financial" in by_acct
    assert {t["file_id"] for t in by_acct["Beacon Financial"]} == {"f2"}


def test_group_drops_unmatched_transcripts():
    transcripts = [
        {"title": "Meeting - Northgate review", "file_id": "f1"},
        {"title": "Internal team sync",         "file_id": "f2"},
    ]
    by_acct = group_transcripts_by_account(transcripts, ["Northgate"])
    assert by_acct == {"Northgate": [transcripts[0]]}


def test_group_handles_empty_inputs():
    assert group_transcripts_by_account([], ["A"]) == {}
    assert group_transcripts_by_account([{"title": "x"}], []) == {}


def test_group_handles_none_input():
    assert group_transcripts_by_account(None, ["A"]) == {}


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v", "--timeout=30"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
