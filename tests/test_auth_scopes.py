"""
tests/test_auth_scopes.py

Regression guard on the Google OAuth scope hierarchy:
SCOPES_READONLY ⊂ SCOPES_WRITE ⊂ SCOPES_SEED.

SCOPES_SEED gates scripts/seed_csm_data.py — if any of gmail.modify,
calendar.events, or drive.file gets dropped, the seeder silently 403s on
every write call. These tests fail fast if that hierarchy is violated.
"""

from product.auth_google import SCOPES_READONLY, SCOPES_SEED, SCOPES_WRITE


def test_scopes_readonly_subset_of_write():
    assert set(SCOPES_READONLY).issubset(set(SCOPES_WRITE))


def test_scopes_write_subset_of_seed():
    assert set(SCOPES_WRITE).issubset(set(SCOPES_SEED))


def test_scopes_seed_includes_gmail_modify():
    assert "https://www.googleapis.com/auth/gmail.modify" in SCOPES_SEED


def test_scopes_seed_includes_calendar_events():
    assert "https://www.googleapis.com/auth/calendar.events" in SCOPES_SEED


def test_scopes_seed_includes_drive_file():
    assert "https://www.googleapis.com/auth/drive.file" in SCOPES_SEED


def test_scopes_write_contains_gmail_send():
    """gmail.send remains the gate for /app/settings/google-actions write unlock."""
    assert "https://www.googleapis.com/auth/gmail.send" in SCOPES_WRITE


def test_scopes_readonly_unchanged():
    """SCOPES_READONLY must include the core read scopes that A.1 unlock
    relies on. May 2026 scope swap: drive.readonly replaced with the
    narrower pair drive.meet.readonly + documents.readonly.
    """
    expected = {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive.meet.readonly",
        "https://www.googleapis.com/auth/documents.readonly",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
    }
    assert expected.issubset(set(SCOPES_READONLY))
