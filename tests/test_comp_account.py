"""
tests/test_comp_account.py

Regression guard for the is_comp_account billing gate override.

Two layers of coverage:

1. Predicate-level: is_subscription_active short-circuits to True when
   is_comp_account=True, regardless of subscription_status or
   trial_ends_at. With is_comp_account=False (or omitted), the
   predicate's prior behavior is preserved across the (active,
   trialing-in-window, trialing-expired, canceled) matrix.

2. Integration: a comp account whose subscription_status='canceled'
   can hit /chat and is NOT blocked by the inline 402 gate. This
   covers the wiring change at the /chat path that previously checked
   sub_status against a hardcoded set.

Standalone-runnable today; auto-discovered under pytest.
Usage:
    python3 tests/test_comp_account.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# STRIPE_PRICE_ID must be set before product.billing is imported
# (transitively pulled in by main).
os.environ.setdefault("STRIPE_PRICE_ID", "price_test_fixture")

from product.subscription import is_subscription_active


# ── Predicate unit tests ─────────────────────────────────────────────────────

def test_comp_account_overrides_canceled_status():
    """A comp account stays active even when subscription_status is
    'canceled'. This is the reviewer/design-partner use case: no
    Stripe subscription, no trial, just a flat override.
    """
    assert is_subscription_active("canceled", None, is_comp_account=True) is True
    print("OK comp + canceled status -> active (True)")


def test_comp_account_overrides_expired_trial():
    """A comp account stays active even with a trial_ends_at in the
    past. This is the reviewer's actual state if they signed up,
    sat through 14 days without paying, and then got comped.
    """
    past = datetime.now(timezone.utc) - timedelta(days=30)
    assert is_subscription_active("trialing", past, is_comp_account=True) is True
    print("OK comp + expired trial -> active (True)")


def test_comp_account_overrides_unknown_status():
    """A comp account is active even when status is 'none' or any
    unrecognized value. The flag is the kill-switch override.
    """
    assert is_subscription_active("none", None, is_comp_account=True) is True
    assert is_subscription_active(None, None, is_comp_account=True) is True
    assert is_subscription_active("past_due", None, is_comp_account=True) is True
    print("OK comp + unknown status -> active (True)")


def test_non_comp_active_status_still_active():
    """Backward compatibility: is_comp_account=False preserves the
    'active' status case.
    """
    assert is_subscription_active("active", None, is_comp_account=False) is True
    past = datetime.now(timezone.utc) - timedelta(days=30)
    assert is_subscription_active("active", past, is_comp_account=False) is True
    print("OK non-comp + active -> active (True)")


def test_non_comp_trialing_in_window_still_active():
    """Backward compatibility: is_comp_account=False preserves the
    'trialing within window' case.
    """
    future = datetime.now(timezone.utc) + timedelta(days=5)
    assert is_subscription_active("trialing", future, is_comp_account=False) is True
    print("OK non-comp + trialing in window -> active (True)")


def test_non_comp_trialing_expired_NOT_active():
    """Backward compatibility: is_comp_account=False preserves the
    'trialing expired' case (gate fires).
    """
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert is_subscription_active("trialing", past, is_comp_account=False) is False
    print("OK non-comp + trialing expired -> NOT active (False)")


def test_non_comp_canceled_NOT_active():
    """Backward compatibility: is_comp_account=False preserves the
    'canceled' case (gate fires).
    """
    future = datetime.now(timezone.utc) + timedelta(days=30)
    assert is_subscription_active("canceled", None, is_comp_account=False) is False
    assert is_subscription_active("canceled", future, is_comp_account=False) is False
    print("OK non-comp + canceled -> NOT active (False)")


def test_default_is_comp_account_is_false():
    """Callers that do not pass the third argument must see the
    pre-flag behavior. Two-argument call is the back-compat contract.
    """
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(days=5)
    # Active stays active
    assert is_subscription_active("active", None) is True
    # Trialing-in-window stays active
    assert is_subscription_active("trialing", future) is True
    # Trialing-expired stays gated
    assert is_subscription_active("trialing", past) is False
    # Canceled stays gated
    assert is_subscription_active("canceled", future) is False
    print("OK two-arg calls preserve prior behavior (default is_comp_account=False)")


# ── Integration test: /chat does not 402 for a comp account ──────────────────

def test_chat_endpoint_allows_comp_account_with_canceled_status():
    """A comp account whose subscription_status is 'canceled' and
    whose trial_ends_at is in the past must still reach /chat. The
    inline 402 gate must call is_subscription_active and short-circuit
    to True via is_comp_account.

    This catches the /chat wiring regression: if /chat reverted to
    checking sub_status against a hardcoded set, this test would 402.
    """
    import main

    # Fake Customer row: canceled, expired trial, but flagged comp.
    fake_row = MagicMock()
    fake_row.subscription_status = "canceled"
    fake_row.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=30)
    fake_row.is_comp_account = True

    fake_db_session = MagicMock()
    fake_db_session.query.return_value.filter_by.return_value.first.return_value = fake_row
    fake_session_factory = MagicMock(return_value=fake_db_session)

    fake_session = {
        "csm_id": "user_comp_test",
        "name": "Comp User",
        "email": "comp@example.com",
    }

    # Make _chat_tooluse return a canned reply so we don't hit Anthropic.
    async def _fake_tooluse(_chat_csm_id, _chat_csm_name, user_msg, history, acct_hint):
        return {"reply": "ok"}

    async def _fake_singleshot(*args, **kwargs):
        return {"reply": "ok"}

    from fastapi.testclient import TestClient
    client = TestClient(main.app, raise_server_exceptions=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=fake_session)), \
         patch("product.database.SessionLocal", fake_session_factory), \
         patch.object(main, "_chat_tooluse", _fake_tooluse), \
         patch.object(main, "_chat_singleshot_legacy", _fake_singleshot):
        resp = client.post("/chat", json={"message": "hello"})

    assert resp.status_code != 402, (
        f"comp account hit the 402 gate at /chat: status={resp.status_code} body={resp.text}"
    )
    assert resp.status_code == 200, (
        f"expected 200 (gate passed, handler ran), got {resp.status_code}: {resp.text}"
    )
    print("OK comp account with canceled status reaches /chat (no 402)")


def test_chat_endpoint_blocks_non_comp_canceled():
    """Inverse guard: a non-comp account with 'canceled' status MUST
    still hit the gate. This guarantees the comp override is not a
    blanket bypass for everyone.
    """
    import main

    fake_row = MagicMock()
    fake_row.subscription_status = "canceled"
    fake_row.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=30)
    fake_row.is_comp_account = False

    fake_db_session = MagicMock()
    fake_db_session.query.return_value.filter_by.return_value.first.return_value = fake_row
    fake_session_factory = MagicMock(return_value=fake_db_session)

    fake_session = {
        "csm_id": "user_canceled_test",
        "name": "Canceled User",
        "email": "canceled@example.com",
    }

    from fastapi.testclient import TestClient
    client = TestClient(main.app, raise_server_exceptions=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=fake_session)), \
         patch("product.database.SessionLocal", fake_session_factory):
        resp = client.post("/chat", json={"message": "hello"})

    assert resp.status_code == 402, (
        f"non-comp canceled account did NOT hit the 402 gate at /chat: "
        f"status={resp.status_code} body={resp.text}"
    )
    print("OK non-comp canceled account is still gated at /chat (402)")


# ── Refactor contract tests ──────────────────────────────────────────────────


def test_chat_unauth_falls_back_to_sarah_c():
    """Unauthenticated /chat requests fall through to the sarah_c demo
    handler, bypassing the subscription gate entirely. Required for
    the /demo prototype, which posts to /chat with only the
    demo_session cookie that _resolve_auth does not consult.

    Pins behavior that was previously only an emergent property of
    test_sarah_c_stays_on_legacy_when_flag_enabled in
    test_chat_tooluse_loop.py.
    """
    import main

    async def _fake_singleshot(*args, **kwargs):
        return {"reply": "sarah_c demo reply"}

    async def _fake_tooluse(*args, **kwargs):
        return {"reply": "sarah_c demo reply"}

    from fastapi.testclient import TestClient
    client = TestClient(main.app, raise_server_exceptions=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "_chat_singleshot_legacy", _fake_singleshot), \
         patch.object(main, "_chat_tooluse", _fake_tooluse):
        resp = client.post("/chat", json={"message": "hello"})

    assert resp.status_code == 200, (
        f"unauthenticated /chat returned {resp.status_code} (expected 200 demo fallback): {resp.text}"
    )
    assert "reply" in resp.json()
    print("OK unauthenticated /chat falls back to sarah_c demo (200)")


def test_chat_uses_require_active_subscription_helper():
    """Authenticated /chat invokes _require_active_subscription exactly
    once. Catches inadvertent re-introduction of an inline duplicate
    gate in addition to the helper call.
    """
    import main

    fake_session = {
        "csm_id": "test_csm_123",
        "name": "Test CSM",
        "email": "test@example.com",
    }

    helper_mock = AsyncMock(return_value=fake_session)

    async def _fake_singleshot(*args, **kwargs):
        return {"reply": "ok"}

    async def _fake_tooluse(*args, **kwargs):
        return {"reply": "ok"}

    from fastapi.testclient import TestClient
    client = TestClient(main.app, raise_server_exceptions=False)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=fake_session)), \
         patch.object(main, "_require_active_subscription", new=helper_mock), \
         patch.object(main, "_chat_singleshot_legacy", _fake_singleshot), \
         patch.object(main, "_chat_tooluse", _fake_tooluse):
        resp = client.post("/chat", json={"message": "hello"})

    assert resp.status_code == 200, (
        f"authenticated /chat returned {resp.status_code}: {resp.text}"
    )
    assert helper_mock.await_count == 1, (
        f"_require_active_subscription was awaited {helper_mock.await_count} times "
        f"(expected exactly 1)"
    )
    print("OK authenticated /chat invokes _require_active_subscription exactly once")


def main_runner():
    tests = [
        # Predicate
        test_comp_account_overrides_canceled_status,
        test_comp_account_overrides_expired_trial,
        test_comp_account_overrides_unknown_status,
        test_non_comp_active_status_still_active,
        test_non_comp_trialing_in_window_still_active,
        test_non_comp_trialing_expired_NOT_active,
        test_non_comp_canceled_NOT_active,
        test_default_is_comp_account_is_false,
        # Integration
        test_chat_endpoint_allows_comp_account_with_canceled_status,
        test_chat_endpoint_blocks_non_comp_canceled,
        # Refactor contract
        test_chat_unauth_falls_back_to_sarah_c,
        test_chat_uses_require_active_subscription_helper,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failures.append(t.__name__)
        except Exception as e:
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failures.append(t.__name__)
    print()
    if failures:
        print(f"FAILED: {len(failures)} of {len(tests)}")
        for n in failures:
            print(f"  - {n}")
        sys.exit(1)
    print(f"PASSED: {len(tests)} of {len(tests)}")


if __name__ == "__main__":
    main_runner()
