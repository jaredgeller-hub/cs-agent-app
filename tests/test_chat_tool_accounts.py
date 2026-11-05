"""
tests/test_chat_tool_accounts.py

Unit tests for the list_my_accounts tool implementation in
product/chat_tools/registry.py.

Mocking strategy:
  - product.database.SessionLocal patched with a MagicMock factory whose
    instances return fake Customer rows from .query().filter_by().first().
  - product.csm_id_validation.safe_data_file patched to return a tmp_path
    that exists and contains a JSON list, or a non-existent path.

Both DB and file are mocked because the production load order is DB
first, file fallback. Tests cover all three branches: DB hit, DB miss
plus file hit, both empty.

Standalone-runnable:
    python3 tests/test_chat_tool_accounts.py
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.chat_tools.registry import dispatch_tool


def _make_session_factory(customer_accounts: list[str] | None) -> MagicMock:
    """Build a SessionLocal-shaped MagicMock.

    Calling the returned factory yields a session whose
    .query(...).filter_by(...).first() returns either a Customer-shaped
    mock or None.
    """
    if customer_accounts is None:
        fake_row = None
    else:
        fake_row = MagicMock(name="customer_row")
        fake_row.accounts = customer_accounts

    fake_session = MagicMock(name="session")
    fake_session.query.return_value.filter_by.return_value.first.return_value = fake_row

    factory = MagicMock(name="SessionLocal", return_value=fake_session)
    return factory


def test_list_my_accounts_returns_db_accounts(tmp_path):
    """DB has a customer with accounts; tool returns them verbatim."""
    factory = _make_session_factory(["Acme Industries", "Beacon Financial", "Northgate Logistics"])

    with patch("product.database.SessionLocal", factory):
        result = asyncio.run(dispatch_tool("list_my_accounts", {}, "user_test123"))

    assert result == {"accounts": ["Acme Industries", "Beacon Financial", "Northgate Logistics"]}


def test_list_my_accounts_falls_back_to_file_when_db_empty(tmp_path):
    """DB has no row for the user; tool reads accounts file from disk."""
    factory = _make_session_factory(None)

    fake_file = tmp_path / "user_test123.json"
    fake_file.write_text(json.dumps({"accounts": ["Solo Acct", "Pair Acct"]}))

    with patch("product.database.SessionLocal", factory), \
         patch("product.csm_id_validation.safe_data_file", return_value=fake_file):
        result = asyncio.run(dispatch_tool("list_my_accounts", {}, "user_test123"))

    assert result == {"accounts": ["Solo Acct", "Pair Acct"]}


def test_list_my_accounts_returns_empty_when_db_and_file_empty(tmp_path):
    """Neither DB nor file has accounts; tool returns the empty list."""
    factory = _make_session_factory(None)

    nonexistent_file = tmp_path / "user_test123.json"
    # Do NOT create the file. path.exists() will be False.

    with patch("product.database.SessionLocal", factory), \
         patch("product.csm_id_validation.safe_data_file", return_value=nonexistent_file):
        result = asyncio.run(dispatch_tool("list_my_accounts", {}, "user_test123"))

    assert result == {"accounts": []}


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
