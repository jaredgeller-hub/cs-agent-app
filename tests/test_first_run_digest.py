"""tests/test_first_run_digest.py

Stage 4 part 2 -- first-run welcome wrapper + helper chips applied at the
SERVING layer (/app/today-digest only). Additive, flag-gated, first-run only.

Invariants under test:
  1. Flag ON + no prior successful DigestLog row before today  ->  response has
     "chips" key with the exact three strings AND digest startswith the welcome
     sentence.
  2. Flag ON + a prior successful DigestLog row before today  ->  response is
     exactly {"digest": <original text>}, no "chips" key, no welcome string.
     Byte-identical proof.
  3. Flag ON + csm_id == "sarah_c"  ->  no wrapper, no chips. Demo guard.
  4. Flag OFF (default) + first-run-eligible user  ->  no wrapper, no chips,
     {"digest": text} exactly.
  5. _is_first_run_digest returns False when SessionLocal is unavailable / on
     DB error  ->  no wrapper.
  6. The three chip strings are NOT members of _DIGEST_TRIGGER_EXACT_PHRASES
     (so a chip tap routes to tool-use chat, not the digest intercept).

Mirrors the in-memory SQLite + autouse SessionLocal rebind pattern from
test_actions_execute_email_draft.py and test_pilot_status.py (the CLAUDE.md
invariant for test files that monkey-patch _db.SessionLocal).

Standalone-runnable:
    python3 tests/test_first_run_digest.py
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer, DigestLog


# ── Test fixture: in-memory SQLite shared across TestClient worker threads ──
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
    """Per the CLAUDE.md invariant: rebind on every test so sibling test
    files that monkey-patch _db.SessionLocal don't leak across collection
    ordering.
    """
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_tables():
    s = _TestSession()
    try:
        s.query(DigestLog).delete()
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


import main  # noqa: E402

client = TestClient(main.app)


# ── Helpers ─────────────────────────────────────────────────────────────────

_FIRST_RUN_INTRO = "Welcome to CS Agent. This is your first daily briefing."
_EXPECTED_CHIPS = [
    "What should I focus on first today?",
    "Show me my at-risk accounts",
    "Which renewals are coming up?",
]


def _seed_customer(clerk_user_id: str, status: str = "active") -> None:
    """Seed a Customer with an active subscription so
    _require_active_subscription passes.
    """
    trial_ends = datetime.now(timezone.utc) + timedelta(days=7)
    s = _TestSession()
    try:
        s.add(Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            trial_ends_at=trial_ends,
        ))
        s.commit()
    finally:
        s.close()


def _seed_digest(csm_id: str, date: str, status: str = "success",
                 text: str = "TODAY DIGEST TEXT") -> None:
    s = _TestSession()
    try:
        s.add(DigestLog(
            id=str(uuid.uuid4()),
            customer_id=csm_id,
            date=date,
            status=status,
            digest_text=text,
            delivered_at=datetime.now(timezone.utc),
        ))
        s.commit()
    finally:
        s.close()


def _authed_session(csm_id: str) -> dict:
    return {"csm_id": csm_id, "name": "Test User",
            "email": f"{csm_id}@example.com"}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


# ────────────────────────────────────────────────────────────────────────────
# 1. Flag ON + no prior digest -> wrapper + chips
# ────────────────────────────────────────────────────────────────────────────

def test_flag_on_first_run_returns_wrapper_and_chips():
    csm_id = "user_firstrun_1"
    _seed_customer(csm_id)
    _seed_digest(csm_id, _today(), status="success",
                 text="### Urgent today\nAcme: 7d silence.")

    with patch.dict(main.os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth",
                      new=AsyncMock(return_value=_authed_session(csm_id))):
        r = client.get("/app/today-digest")

    assert r.status_code == 200, r.text
    body = r.json()

    # chips key present and EXACTLY the three strings (verification evidence).
    assert "chips" in body, (
        f"first-run response must include 'chips' key; got keys={list(body)}"
    )
    assert body["chips"] == _EXPECTED_CHIPS, (
        f"chips must match the fixed first-run list exactly; got {body['chips']!r}"
    )

    # digest starts with the welcome intro.
    assert body["digest"].startswith(_FIRST_RUN_INTRO), (
        f"first-run digest must open with the welcome sentence; "
        f"got first 80 chars: {body['digest'][:80]!r}"
    )
    # original digest content survives intact (suffixed after the wrapper).
    assert "### Urgent today" in body["digest"]
    assert "Acme: 7d silence." in body["digest"]


# ────────────────────────────────────────────────────────────────────────────
# 2. Flag ON + prior digest -> response byte-identical, no chips key
# ────────────────────────────────────────────────────────────────────────────

def test_flag_on_non_first_run_is_byte_identical_no_chips():
    csm_id = "user_returning_2"
    _seed_customer(csm_id)
    # A prior successful digest dated before today -> NOT first-run.
    _seed_digest(csm_id, _yesterday(), status="success", text="prior text")
    # Today's digest -- the one the endpoint serves.
    today_text = "### Urgent today\nReturning user's actual digest body."
    _seed_digest(csm_id, _today(), status="success", text=today_text)

    with patch.dict(main.os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth",
                      new=AsyncMock(return_value=_authed_session(csm_id))):
        r = client.get("/app/today-digest")

    assert r.status_code == 200, r.text
    body = r.json()

    # Byte-identical: response shape is exactly {"digest": today_text}.
    assert body == {"digest": today_text}, (
        f"non-first-run response must be byte-identical to legacy; "
        f"got {body!r}"
    )
    # Chips key MUST be absent.
    assert "chips" not in body, (
        f"non-first-run response must NOT include 'chips' key; got keys={list(body)}"
    )
    # Welcome intro must NOT appear.
    assert _FIRST_RUN_INTRO not in body["digest"]


# ────────────────────────────────────────────────────────────────────────────
# 3. sarah_c is never first-run, even with the flag on
# ────────────────────────────────────────────────────────────────────────────

def test_flag_on_sarah_c_is_byte_identical_no_chips():
    csm_id = "sarah_c"
    _seed_customer(csm_id)
    # No prior digest -- but sarah_c is hard-guarded out of first-run.
    today_text = "Sarah's demo digest."
    _seed_digest(csm_id, _today(), status="success", text=today_text)

    with patch.dict(main.os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth",
                      new=AsyncMock(return_value=_authed_session(csm_id))):
        r = client.get("/app/today-digest")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"digest": today_text}, (
        f"sarah_c must stay byte-identical to legacy under flag; got {body!r}"
    )
    assert "chips" not in body
    assert _FIRST_RUN_INTRO not in body["digest"]


# ────────────────────────────────────────────────────────────────────────────
# 4. Flag OFF + first-run-eligible -> still byte-identical
# ────────────────────────────────────────────────────────────────────────────

def test_flag_off_first_run_eligible_is_byte_identical_no_chips(monkeypatch):
    csm_id = "user_eligible_4"
    _seed_customer(csm_id)
    today_text = "First-run candidate but flag is off."
    _seed_digest(csm_id, _today(), status="success", text=today_text)

    # Ensure the flag is unambiguously off.
    monkeypatch.delenv("DIGEST_GENERATOR_ENABLED", raising=False)

    with patch.object(main, "_resolve_auth",
                      new=AsyncMock(return_value=_authed_session(csm_id))):
        r = client.get("/app/today-digest")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"digest": today_text}, (
        f"flag OFF must produce byte-identical legacy response; got {body!r}"
    )
    assert "chips" not in body
    assert _FIRST_RUN_INTRO not in body["digest"]


# ────────────────────────────────────────────────────────────────────────────
# 5. _is_first_run_digest fails closed on DB error / missing SessionLocal
# ────────────────────────────────────────────────────────────────────────────

def test_is_first_run_digest_returns_false_when_sessionlocal_unavailable(monkeypatch):
    """If SessionLocal is None (DB not configured), _is_first_run_digest
    returns False so the wrapper is NOT applied -- legacy behavior.
    """
    monkeypatch.setattr("product.database.SessionLocal", None)
    assert main._is_first_run_digest("user_no_db") is False


def test_is_first_run_digest_returns_false_on_db_exception(monkeypatch):
    """Any exception inside the query path -> False (no wrapper)."""
    class _BoomSession:
        def __init__(self):
            pass
        def query(self, *a, **kw):
            raise RuntimeError("simulated DB error")
        def close(self):
            pass
    monkeypatch.setattr("product.database.SessionLocal", _BoomSession)
    assert main._is_first_run_digest("user_db_boom") is False


def test_is_first_run_digest_sarah_c_short_circuit():
    """sarah_c bypasses the DB lookup entirely -- always False."""
    assert main._is_first_run_digest("sarah_c") is False


# ────────────────────────────────────────────────────────────────────────────
# 6. The chip strings must not collide with the digest-intercept trigger set
# ────────────────────────────────────────────────────────────────────────────

def test_chip_strings_do_not_collide_with_digest_trigger_phrases():
    """A chip tap routes through the normal /chat tool-use path; the trigger
    set must not promote any chip to the digest intercept. This is enforced
    by _is_digest_request's exact-phrase, lowercased, punctuation-stripped
    match -- apply the same normalization the function uses.
    """
    for chip in _EXPECTED_CHIPS:
        norm = chip.strip().lower().rstrip(".!?")
        assert norm not in main._DIGEST_TRIGGER_EXACT_PHRASES, (
            f"chip {chip!r} (normalized {norm!r}) collides with "
            f"_DIGEST_TRIGGER_EXACT_PHRASES; it would hit the digest "
            f"intercept instead of normal tool-use chat"
        )
        # Belt-and-braces: assert _is_digest_request itself returns False.
        assert main._is_digest_request(chip) is False, (
            f"_is_digest_request({chip!r}) must be False so the chip routes "
            f"to tool-use chat, not the digest intercept"
        )


# ────────────────────────────────────────────────────────────────────────────
# Bonus coverage: filesystem fallback path also receives the wrapper
# ────────────────────────────────────────────────────────────────────────────

def test_filesystem_fallback_first_run_returns_wrapper_and_chips(tmp_path, monkeypatch):
    """When Postgres has no row, the endpoint falls back to the on-disk
    digest. The wrapper + chips logic must apply to that path too.
    """
    csm_id = "user_fs_fallback_6"
    _seed_customer(csm_id)
    # Intentionally do NOT seed a Postgres DigestLog row for today.

    # Write a digest file at the canonical path the endpoint reads.
    import json as _json
    digest_dir = REPO_ROOT / "product" / "data" / "digests" / csm_id
    digest_dir.mkdir(parents=True, exist_ok=True)
    fs_path = digest_dir / f"{_today()}.json"
    fs_text = "Filesystem digest body."
    fs_path.write_text(_json.dumps({"digest_text": fs_text}), encoding="utf-8")

    try:
        with patch.dict(main.os.environ, {"DIGEST_GENERATOR_ENABLED": "true"}), \
             patch.object(main, "_resolve_auth",
                          new=AsyncMock(return_value=_authed_session(csm_id))):
            r = client.get("/app/today-digest")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["chips"] == _EXPECTED_CHIPS
        assert body["digest"].startswith(_FIRST_RUN_INTRO)
        assert fs_text in body["digest"]
    finally:
        try:
            fs_path.unlink()
            # Best-effort dir cleanup if empty.
            digest_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
