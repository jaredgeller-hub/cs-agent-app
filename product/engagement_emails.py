"""
product/engagement_emails.py

Trial engagement email sequence — 4 plain-text emails during a 14-day trial.
Sent via send_system_email (SMTP / sarah_c fallback).
From header set by SYSTEM_EMAIL_ADDRESS env var (default: settings.FROM_EMAIL).
Signed with settings.SENDER_NAME (+ SENDER_TITLE, COMPANY_NAME on day 1).

Dedup: product/data/engagement/{csm_id}_day{N}.sent  (presence = already sent)
"""

from __future__ import annotations

from product.settings import COMPANY_NAME, SENDER_NAME, SENDER_TITLE, SUPPORT_EMAIL

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cs-agent.engagement_emails")

_SUBJECTS: dict[int, str] = {
    1:  "Your CS Agent is live",
    3:  "Quick check-in",
    7:  "One week with CS Agent",
    13: "Your trial ends tomorrow",
}


def _signature(full: bool = False) -> str:
    """Email sign-off. Day 1 adds a second line: "<SENDER_TITLE>, <COMPANY_NAME>"
    (or just COMPANY_NAME when SENDER_TITLE is unset)."""
    if not full:
        return SENDER_NAME
    second = f"{SENDER_TITLE}, {COMPANY_NAME}" if SENDER_TITLE else COMPANY_NAME
    return f"{SENDER_NAME}\n{second}"


def _body(day: int, first_name: str, base_url: str) -> str:
    name = first_name or "there"
    if day == 1:
        return f"""Hey {name},

Your CS Agent is connected and your first digest is ready. Open it up:

{base_url}/app

Try asking CS Agent about any of your accounts. It already has context from your CRM data and knows your calendar.

Tomorrow morning at 8 AM, you'll get your next digest with overnight signals. From there, it just runs.

If anything feels off, reply to this email. I read every one.

{_signature(full=True)}"""

    if day == 3:
        return f"""Hey {name},

You've had a few days with CS Agent. How's it going?

By now you should have received a couple of morning digests. If something looks off or a signal doesn't make sense, that feedback is gold for me.

One thing worth trying if you haven't: after your next call, check what CS Agent sends you. It watches your calendar and follows up with context about the account and asks what happened. The more you tell it, the sharper it gets.

{base_url}/app

{_signature()}"""

    if day == 7:
        return f"""Hey {name},

You're a week in. I'd love to hear what's landing and what's not.

Quick question: has CS Agent caught anything you would have missed? A quiet account, a renewal sneaking up, a meeting you weren't prepped for?

That's the signal that matters to me. If the answer is yes even once, it's working. If not, I want to know why.

Hit reply anytime. Takes 30 seconds and helps me build a better product.

{_signature()}"""

    if day == 13:
        return f"""Hey {name},

Your 14-day CS Agent trial wraps up tomorrow. After that, your digests and alerts will pause.

If CS Agent has earned a spot in your workflow, you can keep it going for $39/month:

{base_url}/app (Settings > Billing)

Your accounts, signals, and history are all saved. Nothing gets deleted.

If it hasn't earned it, I'd genuinely appreciate hearing why. Reply to this email with one sentence and you'll make my week either way.

{_signature()}"""

    raise ValueError(f"No email defined for day {day}")


# ── Dedup helpers ─────────────────────────────────────────────────────────────

def _sent_marker(csm_id: str, day: int) -> Path:
    from product.csm_id_validation import safe_data_file
    return safe_data_file("product/data/engagement", csm_id, suffix=f"_day{day}.sent")


def already_sent(csm_id: str, day: int) -> bool:
    """Return True if the Day N email has already been sent to this user.

    Postgres is the source of truth; filesystem marker is a fallback for
    environments where the DB is unavailable (local dev, import errors).
    """
    try:
        from product.database import SessionLocal, EngagementSend
        if not SessionLocal:
            return _sent_marker(csm_id, day).exists()
        session = SessionLocal()
        try:
            exists = session.query(EngagementSend).filter_by(csm_id=csm_id, day=day).first() is not None
            return exists
        finally:
            session.close()
    except Exception:
        return _sent_marker(csm_id, day).exists()


def mark_sent(csm_id: str, day: int, sent_at: str) -> None:
    """Record that the Day N email was sent so we never send it twice.

    Writes to Postgres first (shared across web + worker containers), then
    writes a filesystem marker as a backward-compat fallback.
    """
    # Postgres primary
    try:
        from product.database import SessionLocal, EngagementSend
        if SessionLocal:
            session = SessionLocal()
            try:
                session.add(EngagementSend(csm_id=csm_id, day=day))
                session.commit()
            finally:
                session.close()
    except Exception:
        pass
    # Filesystem fallback (backward compat)
    marker = _sent_marker(csm_id, day)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"sent_at": sent_at}), encoding="utf-8")
    except (PermissionError, OSError):
        pass


# ── Public API ────────────────────────────────────────────────────────────────

def send_engagement_email(
    csm_id: str,
    email: str,
    first_name: str,
    day: int,
    base_url: str | None = None,
) -> dict:
    """Send the trial engagement email for the given day (1, 3, 7, or 13).

    Uses sarah_c's Gmail credentials as the sending account.
    Idempotent — returns {"status": "skipped"} if already sent.

    Returns:
        {"status": "sent",    "sent_at": "<iso>"}
        {"status": "skipped", "reason":  "<reason>"}
    """
    if day not in _SUBJECTS:
        return {"status": "skipped", "reason": f"no_email_for_day_{day}"}

    if already_sent(csm_id, day):
        return {"status": "skipped", "reason": "already_sent"}

    if not email:
        return {"status": "skipped", "reason": "no_email"}

    from product.digest.delivery import send_system_email

    if base_url is None:
        from product.settings import PUBLIC_BASE_URL
        base_url = PUBLIC_BASE_URL
    _text = _body(day, first_name, base_url)
    delivered = send_system_email(
        to=email,
        subject=_SUBJECTS[day],
        body_html=f"<pre style='font-family:sans-serif;white-space:pre-wrap;'>{_text}</pre>",
        body_plain=_text,
    )

    if not delivered:
        # Recipient was skipped (e.g. @noreply.clerk fallback). Do NOT mark_sent,
        # otherwise a real email arriving later (after the user updates their
        # address) would be suppressed by already_sent.
        return {"status": "skipped", "reason": "recipient_skipped"}

    sent_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    mark_sent(csm_id, day, sent_at)
    logger.info("Day %d sent to %s (csm_id=%s)", day, email, csm_id)
    return {"status": "sent", "sent_at": sent_at}


# ── Digest failure alerts ────────────────────────────────────────────────────

def send_digest_failure_alert(user_id: str, user_email: str, error: str) -> None:
    """Send an email to the founder when a user's digest generation fails.

    Uses the same SMTP transport as engagement emails. Best-effort — never
    raises, since the caller is already in an exception handler.
    """
    from product.digest.delivery import send_system_email

    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    subject = f"[CS Agent] Digest failed for {user_email or user_id}"
    body = (
        f"Digest generation failed.\n\n"
        f"User ID: {user_id}\n"
        f"Email: {user_email or '(unknown)'}\n"
        f"Error: {error}\n"
        f"Timestamp: {timestamp}\n"
    )
    try:
        delivered = send_system_email(
            to=SUPPORT_EMAIL,
            subject=subject,
            body_html=f"<pre style='font-family:sans-serif;white-space:pre-wrap;'>{body}</pre>",
            body_plain=body,
        )
        if delivered:
            logger.info("Digest failure alert sent for %s", user_id)
        else:
            logger.info("Digest failure alert skipped for %s (noreply recipient)", user_id)
    except Exception as exc:
        logger.warning("Could not send digest failure alert for %s: %s", user_id, exc)
