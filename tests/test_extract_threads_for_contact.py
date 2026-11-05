"""
tests/test_extract_threads_for_contact.py

Unit test for extract_threads_for_contact in product/signals/gmail.py.
Mocks _load_credentials, build, and _fetch_threads to drive a deterministic
flow without hitting the live Gmail API.

Standalone-runnable:
    python3 tests/test_extract_threads_for_contact.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from product.settings import DEMO_EMAIL  # noqa: E402

import pytest

from product.signals import gmail as gmail_module


def _msg(internal_date_ms: int, from_email: str) -> dict:
    """Build a Gmail message stub with internalDate and a From header."""
    return {
        "internalDate": str(internal_date_ms),
        "payload": {"headers": [{"name": "From", "value": from_email}]},
    }


def test_extract_threads_for_contact_returns_metadata_shape():
    """Wrapper returns the expected metadata-only shape; raw messages are dropped."""
    contact_email = "marcus@m.example"
    fake_threads = [
        {
            "thread_id": "t1",
            "subject": "Renewal kickoff",
            "last_from": "marcus@m.example",
            "date": "2026-04-30T12:00:00+00:00",
            "messages": [
                _msg(1714400000000, DEMO_EMAIL),
                _msg(1714486400000, "marcus@m.example"),
            ],
        },
    ]

    with patch.object(
        gmail_module, "_load_credentials", return_value=MagicMock(name="creds")
    ), patch.object(
        gmail_module, "build", return_value=MagicMock(name="service")
    ), patch.object(
        gmail_module, "_fetch_threads", return_value=fake_threads
    ):
        result = gmail_module.extract_threads_for_contact("user_x", contact_email, days=14)

    assert isinstance(result, list)
    assert len(result) == 1
    out = result[0]
    assert out["thread_id"] == "t1"
    assert out["subject"] == "Renewal kickoff"
    assert out["last_from"] == "marcus@m.example"
    assert out["last_date"] == "2026-04-30T12:00:00+00:00"
    assert out["message_count"] == 2
    assert out["direction"] in ("sent", "received")
    assert isinstance(out["got_reply"], bool)
    # raw messages list must NOT leak into the wrapper output
    assert "messages" not in out


def test_extract_threads_for_contact_rejects_invalid_email():
    """Email shape validation: no @ returns empty without touching Gmail."""
    with patch.object(
        gmail_module, "_load_credentials"
    ) as load_creds:
        result = gmail_module.extract_threads_for_contact("user_x", "not-an-email", days=14)
    assert result == []
    load_creds.assert_not_called()


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
