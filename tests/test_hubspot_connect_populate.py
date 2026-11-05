"""
tests/test_hubspot_connect_populate.py

Regression guards for F-5: connecting HubSpot auto-populates the CSM's book of
business (Customer.accounts) from owner-filtered HubSpot companies.

Covers _populate_hubspot_book_of_business (main.py):
  - non-empty owner accounts are written to Customer.accounts (names only)
  - an empty result does NOT overwrite an existing book (protects against a
    transient MCP failure or a reconnect wiping a manually-set list)
  - blank/missing names are filtered out

The sarah_c demo guard lives inline in hubspot_auth_callback
(`if csm_id != "sarah_c"`) and is verified by inspection — exercising it would
require mocking the full OAuth-state + code-exchange chain.

Standalone-runnable:
    python3 tests/test_hubspot_connect_populate.py
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer
import product.connectors.hubspot_crm as _hubspot_crm

import main


# ── In-memory SQLite, monkey-patched SessionLocal ────────────────────────────
_test_engine = create_engine(
    "sqlite:///:memory:",
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _bind_session_to_this_files_engine():
    """Rebind SessionLocal before every test — CLAUDE.md invariant for files
    that monkey-patch _db.SessionLocal."""
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_customers():
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


def _seed_customer(clerk_user_id="u1", accounts=None):
    s = _TestSession()
    try:
        s.add(Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status="active",
            accounts=accounts,
        ))
        s.commit()
    finally:
        s.close()


def _get_accounts(clerk_user_id="u1"):
    s = _TestSession()
    try:
        return s.query(Customer).filter_by(clerk_user_id=clerk_user_id).first().accounts
    finally:
        s.close()


def _connector_mock(accounts):
    inst = MagicMock()
    inst.get_accounts.return_value = accounts
    return MagicMock(return_value=inst)


def test_populates_accounts_from_owner_companies():
    _seed_customer("u1", accounts=None)
    conn = _connector_mock([
        {"name": "Acme Corp", "hubspot_id": 1},
        {"name": "Beta LLC", "hubspot_id": 2},
        {"hubspot_id": 3},  # no name — must be filtered out
    ])
    with patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn):
        count = main._populate_hubspot_book_of_business("u1")
    assert count == 2
    assert _get_accounts("u1") == ["Acme Corp", "Beta LLC"]
    print("✓ populates Customer.accounts from owner companies (names only)")


def test_empty_result_does_not_overwrite_existing_book():
    _seed_customer("u1", accounts=["Existing Co", "Another Co"])
    conn = _connector_mock([])  # transient failure OR genuinely empty
    with patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn):
        count = main._populate_hubspot_book_of_business("u1")
    assert count == 0
    assert _get_accounts("u1") == ["Existing Co", "Another Co"], "must not wipe an existing book"
    print("✓ empty result leaves an existing book untouched")


def test_all_blank_names_does_not_overwrite():
    _seed_customer("u1", accounts=["Existing Co"])
    conn = _connector_mock([{"hubspot_id": 1}, {"name": ""}])
    with patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn):
        count = main._populate_hubspot_book_of_business("u1")
    assert count == 0
    assert _get_accounts("u1") == ["Existing Co"]
    print("✓ all-blank names treated as empty, no overwrite")


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.run(["python3", "-m", "pytest", __file__, "-v"], cwd=str(REPO_ROOT)).returncode)
