"""
tests/test_auth_google.py

Regression guards for A.1 Commits 1 + 5:

- refresh_token preservation in save_google_token() — Google re-auth
  responses sometimes omit refresh_token; unconditional overwrite with
  None would destroy offline-access capability. (Commit 1)
- SCOPES_WRITE scope-shape contract (includes gmail.send; excludes
  calendar.events (removed April 23 — feature not built), gmail.modify,
  and broader calendar scopes). Originally SCOPES_SEND (gmail.send only)
  in Commit 1, renamed + expanded in Commit 5 to include calendar.events,
  narrowed back to gmail.send only in the April 23 scope hygiene commit.
- SCOPES_FULL non-existence guard — prevents accidental reintroduction
  of the scope set that was retired in A.1 Commit 1 (the original
  gmail.modify-based approach didn't match the GCP consent screen
  granularity we want).

Sister file: tests/test_settings_google_actions.py covers the
endpoint behavior (GET/POST /app/settings/google-actions and the
/auth/google/start?scope=write redirect). This file covers only
the scope-set shape and the save_google_token refresh-token contract.

Standalone-runnable and pytest-discoverable (same pattern as the other
tests/test_*.py files in this repo).
    python3 tests/test_auth_google.py
"""

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product import auth_google


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_token_store(monkeypatch):
    """Replace product.token_store.save_token and load_token with in-memory
    substitutes so tests don't touch the real DB, filesystem, or shared
    module state across runs.
    """
    store = {}

    def fake_save(csm_id, provider, token_data):
        store[(csm_id, provider)] = dict(token_data)

    def fake_load(csm_id, provider):
        return store.get((csm_id, provider))

    monkeypatch.setattr("product.token_store.save_token", fake_save)
    monkeypatch.setattr("product.token_store.load_token", fake_load)

    # Suppress the filesystem fallback — safe_data_file write either fails
    # silently or writes to /tmp, but the test_auth_google flow doesn't need
    # to exercise it. The Postgres-mocked path is the invariant under test.
    yield store


# ── SCOPES shape tests ───────────────────────────────────────────────────────

def test_scopes_write_contains_gmail_send_and_excludes_retired_scopes():
    """SCOPES_WRITE is the Google-actions elevation scope set. After the
    April 23 scope hygiene commit, it contains gmail.send on top of
    readonly — nothing else. calendar.events was retired because the
    calendar write feature is not built; it will be re-added via
    supplementary OAuth verification when that feature ships. Must NOT
    include gmail.modify (broader than needed) or any full calendar scope.
    """
    scopes = auth_google.SCOPES_WRITE
    assert "https://www.googleapis.com/auth/gmail.send" in scopes
    assert "https://www.googleapis.com/auth/calendar.events" not in scopes, (
        "calendar.events must not be in SCOPES_WRITE — feature not built, "
        "scope retired April 23"
    )
    assert "https://www.googleapis.com/auth/gmail.modify" not in scopes
    assert "https://www.googleapis.com/auth/calendar" not in scopes
    # Sanity: SCOPES_WRITE is a superset of SCOPES_READONLY
    for scope in auth_google.SCOPES_READONLY:
        assert scope in scopes, f"SCOPES_READONLY scope missing from SCOPES_WRITE: {scope}"


def test_scopes_readonly_uses_narrow_meet_and_docs_scopes():
    """Regression guard: SCOPES_READONLY must declare the narrow
    drive.meet.readonly + documents.readonly pair (May 2026 scope swap
    accepted by Google's OAuth reviewer), never the broader drive.readonly
    or the full drive scope (read+write+delete, the #1 OAuth verification
    rejection risk). Full drive was declared pre-April 23 and narrowed in
    the scope hygiene commit; drive.readonly was narrowed further in the
    May 2026 swap once we confirmed every live Drive callsite reads only
    Meet artifacts and Google Docs.
    """
    scopes = auth_google.SCOPES_READONLY
    assert "https://www.googleapis.com/auth/drive.meet.readonly" in scopes
    assert "https://www.googleapis.com/auth/documents.readonly" in scopes
    assert "https://www.googleapis.com/auth/drive.readonly" not in scopes, (
        "drive.readonly is back in SCOPES_READONLY — this is the scope we "
        "swapped away from in May 2026. Our live Drive callsites read only "
        "Meet artifacts (drive.meet.readonly) and Google Docs "
        "(documents.readonly); drive.readonly is overreach."
    )
    assert "https://www.googleapis.com/auth/drive" not in scopes, (
        "Full 'drive' scope is back in SCOPES_READONLY — this is scope "
        "overreach (read+write+delete everything in user's Drive) and will "
        "fail OAuth verification."
    )


def test_scopes_full_is_removed_from_module():
    """Regression guard: SCOPES_FULL was deleted along with
    /admin/google-reauth. If a future change reintroduces it (by habit,
    copy-paste, or review slip), this test fails and forces the author
    to justify why SCOPES_FULL is back.
    """
    assert not hasattr(auth_google, "SCOPES_FULL"), (
        "SCOPES_FULL is back on product.auth_google — it was removed in A.1 "
        "Commit 1 because GCP OAuth consent screen does not list gmail.modify "
        "or calendar.events. Reintroducing it resurrects a scope set that "
        "does not function in production."
    )


# ── refresh_token preservation tests ─────────────────────────────────────────

def test_save_google_token_preserves_prior_refresh_token_when_incoming_missing(_isolate_token_store):
    """Google re-auth returned a response without refresh_token. Prior row
    had a valid refresh_token. The save must keep the prior value.
    """
    store = _isolate_token_store
    # Seed a prior saved credentials row with a refresh_token
    store[("user_reauth_1", "google")] = {
        "token": "old_access_token",
        "refresh_token": "prior_refresh_token_to_preserve",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }

    # Simulate a Google re-auth response WITHOUT refresh_token
    auth_google.save_google_token("user_reauth_1", {
        "access_token": "new_access_token",
        # no refresh_token — this is the bug scenario
        "scope": " ".join(auth_google.SCOPES_WRITE),
    })

    saved = store[("user_reauth_1", "google")]
    assert saved["refresh_token"] == "prior_refresh_token_to_preserve", (
        f"refresh_token was NOT preserved; got {saved['refresh_token']!r}"
    )
    assert saved["token"] == "new_access_token"


def test_save_google_token_warns_when_first_save_has_no_refresh_token(_isolate_token_store, caplog):
    """First save (no prior row) with a response that omits refresh_token
    is unusual — Google's initial authorization response always includes
    one when prompt=consent is set. Log a WARNING so this surfaces in
    production logs if it ever happens.
    """
    caplog.set_level(logging.WARNING, logger="cs-agent.auth_google")

    auth_google.save_google_token("user_first_save_no_refresh", {
        "access_token": "only_access_token",
        # no refresh_token AND no prior row
        "scope": " ".join(auth_google.SCOPES_READONLY),
    })

    # Warning fired
    warnings = [r for r in caplog.records
                if r.levelname == "WARNING"
                and "refresh_token missing on first save" in r.getMessage()]
    assert warnings, (
        f"expected a WARNING about first-save-without-refresh-token; "
        f"got: {[r.getMessage() for r in caplog.records]!r}"
    )

    # Row was still saved (we don't block; the access_token has short-term value)
    saved = _isolate_token_store[("user_first_save_no_refresh", "google")]
    assert saved["token"] == "only_access_token"
    assert saved["refresh_token"] is None


def test_save_google_token_overwrites_when_incoming_refresh_is_truthy(_isolate_token_store):
    """Normal path: incoming response has a fresh refresh_token. The save
    must overwrite the prior value. No preservation when incoming is
    present.
    """
    store = _isolate_token_store
    store[("user_fresh_refresh", "google")] = {
        "token": "old_access",
        "refresh_token": "old_refresh_should_be_replaced",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }

    auth_google.save_google_token("user_fresh_refresh", {
        "access_token": "new_access",
        "refresh_token": "new_refresh_from_google",
        "scope": " ".join(auth_google.SCOPES_WRITE),
    })

    saved = store[("user_fresh_refresh", "google")]
    assert saved["refresh_token"] == "new_refresh_from_google", (
        f"fresh refresh_token not written; got {saved['refresh_token']!r}"
    )
    assert saved["token"] == "new_access"


def main():
    """Standalone runner (same pattern as other tests/test_*.py files)."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
