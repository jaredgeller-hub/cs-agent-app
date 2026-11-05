"""
tests/test_engagement_emails.py

Regression guards for Track D[F5.3] Stage 2 (Sprint 5 Week 1):

  A. send_system_email return value
     - Returns True on successful SMTP / Gmail API send
     - Returns False on @noreply.clerk recipient skip
     - Raises on transport failure (existing behavior)

  B. send_engagement_email gates mark_sent on the bool return
     - mark_sent invoked when delivered=True
     - mark_sent NOT invoked when delivered=False
       (otherwise a real email landing later would be suppressed)

  C. _process_trial_emails catch-up loop
     - Walks (1, 3, 7, 13) ascending, filters by day <= _days
     - Day 13 excluded once _days >= 14 (copy says "ends tomorrow")
     - Idempotency via already_sent (no second send for the same day)
     - Regression: a Day-N row in engagement_sends suppresses re-send
       even if upstream send_system_email had previously skipped. This
       only matters because the send-path fix (scope A) ensures we no
       longer mark_sent on skip going forward, but historical rows can
       still exist.

Isolation: in-memory SQLite engine, SessionLocal monkey-patched onto
product.database (same pattern as tests/test_expire_trials.py and
tests/test_oauth_state.py).

Standalone-runnable:
    python3 tests/test_engagement_emails.py
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import product.database as _db
from product.database import Base, Customer, EngagementSend


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────

_test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession

# Sibling test files (test_oauth_state.py, test_expire_trials.py) also rebind
# _db.SessionLocal at module import. Under pytest, whichever imports last wins,
# so we re-bind before every test in this file via an autouse fixture.
try:
    import pytest

    @pytest.fixture(autouse=True)
    def _bind_session_to_this_files_engine():
        _db.SessionLocal = _TestSession
        yield
except ImportError:
    pass


# ── Helpers ──────────────────────────────────────────────────────────────────

def _reset_tables() -> None:
    session = _TestSession()
    try:
        session.query(EngagementSend).delete()
        session.query(Customer).delete()
        session.commit()
    finally:
        session.close()


def _make_customer(
    clerk_user_id: str,
    email: str,
    days_old: int,
    name: str = "Test User",
) -> None:
    session = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=email,
            name=name,
            clerk_user_id=clerk_user_id,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        )
        session.add(cust)
        session.commit()
    finally:
        session.close()


def _seed_engagement_send(csm_id: str, day: int) -> None:
    """Mark a (csm_id, day) as already sent. Used to test idempotency and to
    simulate the historical-buggy-mark_sent regression scenario."""
    session = _TestSession()
    try:
        session.add(EngagementSend(csm_id=csm_id, day=day))
        session.commit()
    finally:
        session.close()


class _Tracker:
    """Captures calls to send_system_email in order. Optionally returns False
    for specific recipients to simulate the @noreply.clerk skip path."""

    def __init__(self, skip_recipients: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, str]] = []  # (to, subject)
        self.skip_recipients = skip_recipients

    def __call__(self, to: str, subject: str, body_html: str, body_plain: str = "") -> bool:
        self.calls.append((to, subject))
        if to in self.skip_recipients:
            return False
        return True


# ── A. send_system_email return value ────────────────────────────────────────

def test_send_system_email_returns_false_on_noreply_recipient():
    from product.digest.delivery import send_system_email
    result = send_system_email(
        to="user_abc@noreply.clerk",
        subject="ignored",
        body_html="<p>x</p>",
        body_plain="x",
    )
    assert result is False, f"noreply recipient must return False, got {result!r}"
    print("ok send_system_email returns False on @noreply.clerk recipient")


def test_send_system_email_returns_true_on_successful_send(monkeypatch=None):
    """Patch the SMTP context to a no-op so we exercise the True return path
    without a network call. We monkeypatch via the module attribute since the
    standalone runner does not provide pytest's monkeypatch fixture."""
    import os
    from product.digest import delivery as _delivery

    class _FakeSMTP:
        def __init__(self, *_a, **_kw): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def login(self, *_a, **_kw): pass
        def sendmail(self, *_a, **_kw): pass

    saved_smtp = _delivery.smtplib.SMTP_SSL
    saved_user = os.environ.get("SYSTEM_EMAIL_SMTP_USER")
    saved_pass = os.environ.get("SYSTEM_EMAIL_PASSWORD")
    _delivery.smtplib.SMTP_SSL = _FakeSMTP  # type: ignore[assignment]
    os.environ["SYSTEM_EMAIL_SMTP_USER"] = "test@example.com"
    os.environ["SYSTEM_EMAIL_PASSWORD"] = "fake_app_password"
    try:
        result = _delivery.send_system_email(
            to="real.user@example.com",
            subject="hello",
            body_html="<p>x</p>",
            body_plain="x",
        )
        assert result is True, f"successful send must return True, got {result!r}"
    finally:
        _delivery.smtplib.SMTP_SSL = saved_smtp  # type: ignore[assignment]
        if saved_user is None:
            os.environ.pop("SYSTEM_EMAIL_SMTP_USER", None)
        else:
            os.environ["SYSTEM_EMAIL_SMTP_USER"] = saved_user
        if saved_pass is None:
            os.environ.pop("SYSTEM_EMAIL_PASSWORD", None)
        else:
            os.environ["SYSTEM_EMAIL_PASSWORD"] = saved_pass
    print("ok send_system_email returns True on successful SMTP send")


def test_send_system_email_raises_on_smtp_failure():
    """Transport errors must propagate; callers decide retry behavior."""
    import os
    from product.digest import delivery as _delivery

    class _BoomSMTP:
        def __init__(self, *_a, **_kw): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def login(self, *_a, **_kw):
            raise RuntimeError("simulated SMTP login failure")
        def sendmail(self, *_a, **_kw): pass

    saved_smtp = _delivery.smtplib.SMTP_SSL
    saved_user = os.environ.get("SYSTEM_EMAIL_SMTP_USER")
    saved_pass = os.environ.get("SYSTEM_EMAIL_PASSWORD")
    _delivery.smtplib.SMTP_SSL = _BoomSMTP  # type: ignore[assignment]
    os.environ["SYSTEM_EMAIL_SMTP_USER"] = "test@example.com"
    os.environ["SYSTEM_EMAIL_PASSWORD"] = "fake_app_password"
    raised = False
    try:
        try:
            _delivery.send_system_email(
                to="real.user@example.com",
                subject="hello",
                body_html="<p>x</p>",
                body_plain="x",
            )
        except RuntimeError:
            raised = True
    finally:
        _delivery.smtplib.SMTP_SSL = saved_smtp  # type: ignore[assignment]
        if saved_user is None:
            os.environ.pop("SYSTEM_EMAIL_SMTP_USER", None)
        else:
            os.environ["SYSTEM_EMAIL_SMTP_USER"] = saved_user
        if saved_pass is None:
            os.environ.pop("SYSTEM_EMAIL_PASSWORD", None)
        else:
            os.environ["SYSTEM_EMAIL_PASSWORD"] = saved_pass
    assert raised, "SMTP transport failure must propagate to caller"
    print("ok send_system_email raises on SMTP transport failure")


# ── B. send_engagement_email gates mark_sent on bool return ──────────────────

def test_send_engagement_email_marks_sent_when_delivered():
    _reset_tables()
    from product import engagement_emails as _ee

    tracker = _Tracker()
    saved = _ee.send_system_email if hasattr(_ee, "send_system_email") else None
    # send_engagement_email imports send_system_email lazily inside the function,
    # so patch the source module instead.
    from product.digest import delivery as _delivery
    saved_real = _delivery.send_system_email
    _delivery.send_system_email = tracker  # type: ignore[assignment]
    try:
        result = _ee.send_engagement_email(
            csm_id="user_delivered",
            email="real.user@example.com",
            first_name="Real",
            day=1,
        )
    finally:
        _delivery.send_system_email = saved_real  # type: ignore[assignment]

    assert result["status"] == "sent", f"expected sent, got {result}"
    assert tracker.calls == [("real.user@example.com", _ee._SUBJECTS[1])], (
        f"expected one tracked call, got {tracker.calls}"
    )

    # mark_sent must have written a row
    session = _TestSession()
    try:
        row = session.query(EngagementSend).filter_by(csm_id="user_delivered", day=1).first()
        assert row is not None, "mark_sent should have inserted a row when delivered=True"
    finally:
        session.close()
    print("ok send_engagement_email writes engagement_sends row when delivered=True")


def test_send_engagement_email_does_not_mark_sent_when_skipped():
    _reset_tables()
    from product import engagement_emails as _ee
    from product.digest import delivery as _delivery

    tracker = _Tracker(skip_recipients=("user_xyz@noreply.clerk",))
    saved_real = _delivery.send_system_email
    _delivery.send_system_email = tracker  # type: ignore[assignment]
    try:
        result = _ee.send_engagement_email(
            csm_id="user_skipped",
            email="user_xyz@noreply.clerk",
            first_name="Skip",
            day=1,
        )
    finally:
        _delivery.send_system_email = saved_real  # type: ignore[assignment]

    assert result["status"] == "skipped", f"expected skipped, got {result}"
    # send_system_email was still called once (it makes the skip decision)
    assert len(tracker.calls) == 1, f"expected one tracked call, got {tracker.calls}"

    session = _TestSession()
    try:
        row = session.query(EngagementSend).filter_by(csm_id="user_skipped", day=1).first()
        assert row is None, (
            "engagement_sends row must NOT exist when send_system_email returned False; "
            "otherwise a real email landing later would be suppressed by already_sent."
        )
    finally:
        session.close()
    print("ok send_engagement_email leaves engagement_sends empty when delivered=False")


# ── C. _process_trial_emails catch-up logic ──────────────────────────────────

def _run_selector_with_tracker() -> _Tracker:
    """Run _process_trial_emails with send_system_email replaced by a tracker.
    Returns the tracker so the test can inspect call order."""
    from product.digest import delivery as _delivery
    tracker = _Tracker()
    saved_real = _delivery.send_system_email
    _delivery.send_system_email = tracker  # type: ignore[assignment]
    try:
        from main import _process_trial_emails
        _process_trial_emails()
    finally:
        _delivery.send_system_email = saved_real  # type: ignore[assignment]
    return tracker


def _days_in_calls(tracker: _Tracker, email: str) -> list[int]:
    """Extract the day numbers (1, 3, 7, 13) from tracker.calls for one email,
    in call order. Subjects are pulled from _SUBJECTS so this stays stable."""
    from product.engagement_emails import _SUBJECTS
    subj_to_day = {v: k for k, v in _SUBJECTS.items()}
    return [subj_to_day[subj] for to, subj in tracker.calls if to == email]


def test_selector_days_zero_sends_nothing():
    _reset_tables()
    _make_customer("u_d0", "u_d0@example.com", days_old=0)
    tracker = _run_selector_with_tracker()
    assert _days_in_calls(tracker, "u_d0@example.com") == [], (
        f"expected no calls for _days=0, got {tracker.calls}"
    )
    print("ok _days=0 -> no emails")


def test_selector_days_one_sends_day_one():
    _reset_tables()
    _make_customer("u_d1", "u_d1@example.com", days_old=1)
    tracker = _run_selector_with_tracker()
    assert _days_in_calls(tracker, "u_d1@example.com") == [1]
    print("ok _days=1 -> [1]")


def test_selector_days_five_sends_one_then_three():
    _reset_tables()
    _make_customer("u_d5", "u_d5@example.com", days_old=5)
    tracker = _run_selector_with_tracker()
    assert _days_in_calls(tracker, "u_d5@example.com") == [1, 3], (
        "ascending order required for catch-up"
    )
    print("ok _days=5 -> [1, 3] in ascending order")


def test_selector_days_eight_sends_one_three_seven():
    _reset_tables()
    _make_customer("u_d8", "u_d8@example.com", days_old=8)
    tracker = _run_selector_with_tracker()
    assert _days_in_calls(tracker, "u_d8@example.com") == [1, 3, 7]
    print("ok _days=8 -> [1, 3, 7]")


def test_selector_days_thirteen_sends_full_sequence():
    _reset_tables()
    _make_customer("u_d13", "u_d13@example.com", days_old=13)
    tracker = _run_selector_with_tracker()
    assert _days_in_calls(tracker, "u_d13@example.com") == [1, 3, 7, 13]
    print("ok _days=13 -> [1, 3, 7, 13]")


def test_selector_days_fourteen_skips_day_thirteen():
    _reset_tables()
    _make_customer("u_d14", "u_d14@example.com", days_old=14)
    tracker = _run_selector_with_tracker()
    days = _days_in_calls(tracker, "u_d14@example.com")
    assert days == [1, 3, 7], (
        f"Day 13 must be skipped on _days>=14 ('ends tomorrow' copy is wrong); got {days}"
    )
    print("ok _days=14 -> [1, 3, 7] (Day 13 excluded by copy cutoff)")


def test_selector_idempotent_when_day_one_already_sent():
    _reset_tables()
    _make_customer("u_d5_partial", "u_d5p@example.com", days_old=5)
    _seed_engagement_send("u_d5_partial", 1)  # Day 1 already sent
    tracker = _run_selector_with_tracker()
    days = _days_in_calls(tracker, "u_d5p@example.com")
    assert days == [3], (
        f"Day 1 must NOT re-send when already in engagement_sends; got {days}"
    )
    print("ok _days=5 with Day 1 already_sent -> [3]")


def test_selector_regression_historical_buggy_marksent_blocks_resend():
    """Regression test for the Stage 2 send-path fix.

    Before the fix, send_engagement_email called mark_sent unconditionally,
    so a Day-1 noreply skip still wrote an engagement_sends row. Going forward
    that row will not be written. But historical rows can still exist.

    This test confirms that already_sent suppresses Day 1 once a row exists,
    regardless of how that row got there. The catch-up loop's idempotency
    contract relies on this guarantee.
    """
    _reset_tables()
    _make_customer("u_hist", "u_hist@example.com", days_old=5)
    # Simulate a historically-buggy mark_sent: a Day-1 row exists even though
    # the user never actually received Day 1 (e.g. previously skipped via
    # @noreply.clerk under the pre-fix code path).
    _seed_engagement_send("u_hist", 1)
    tracker = _run_selector_with_tracker()
    days = _days_in_calls(tracker, "u_hist@example.com")
    assert days == [3], (
        f"Historical engagement_sends row must suppress Day 1; got {days}. "
        "This depends on the send-path fix in scope A landing in the same commit "
        "so that future skips do NOT add new spurious rows."
    )
    print("ok historical mark_sent regression: Day 1 suppressed, Day 3 still sent")


# ── Runner ───────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        test_send_system_email_returns_false_on_noreply_recipient,
        test_send_system_email_returns_true_on_successful_send,
        test_send_system_email_raises_on_smtp_failure,
        test_send_engagement_email_marks_sent_when_delivered,
        test_send_engagement_email_does_not_mark_sent_when_skipped,
        test_selector_days_zero_sends_nothing,
        test_selector_days_one_sends_day_one,
        test_selector_days_five_sends_one_then_three,
        test_selector_days_eight_sends_one_three_seven,
        test_selector_days_thirteen_sends_full_sequence,
        test_selector_days_fourteen_skips_day_thirteen,
        test_selector_idempotent_when_day_one_already_sent,
        test_selector_regression_historical_buggy_marksent_blocks_resend,
    ]
    failures: list[str] = []
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
    main()
