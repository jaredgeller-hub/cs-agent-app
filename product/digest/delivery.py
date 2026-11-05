"""
product/digest/delivery.py

Email delivery for the CS Agent morning digest.
Sends the digest to the CSM via Gmail API using OAuth credentials.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger("cs-agent.delivery")

DATA_DIR = Path("product/data")

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


# ── Config ───────────────────────────────────────────────────────────────────

TOKEN_DIR = Path(__file__).parent.parent.parent / "tokens"
from product.settings import FROM_EMAIL  # noqa: E402  (default sender; SYSTEM_EMAIL_ADDRESS overrides)


# ── System email (SMTP) ───────────────────────────────────────────────────────

def send_system_email(
    to: str,
    subject: str,
    body_html: str,
    body_plain: str = "",
) -> bool:
    """Send a system-originated email via SMTP.

    Requires env vars:
      - SYSTEM_EMAIL_SMTP_USER   (the SMTP login, e.g. an app-password account)
      - SYSTEM_EMAIL_PASSWORD    (app password)
      - SYSTEM_EMAIL_ADDRESS     (From address, defaults to settings.FROM_EMAIL)

    Returns:
        True  if the email was actually transmitted to the SMTP server.
        False if delivery was deliberately skipped (e.g. recipient matches the
              @noreply.clerk fallback). Callers must NOT treat False as a send.

    Raises:
        RuntimeError if SMTP env vars are missing — the Stage 2a Gmail-API
            fallback (sarah_c-bound) was removed. Operators must configure
            SYSTEM_EMAIL_SMTP_USER + SYSTEM_EMAIL_PASSWORD.
        Any SMTP transport error from smtplib. Callers decide retry behavior.
    """
    if "@noreply.clerk" in to:
        logger.info("Skipping system email, recipient is noreply fallback: %s", to)
        return False

    smtp_user    = os.environ.get("SYSTEM_EMAIL_SMTP_USER", "")
    smtp_pass    = os.environ.get("SYSTEM_EMAIL_PASSWORD", "")
    from_address = os.environ.get("SYSTEM_EMAIL_ADDRESS", FROM_EMAIL)

    if not (smtp_user and smtp_pass):
        raise RuntimeError(
            "send_system_email requires SYSTEM_EMAIL_SMTP_USER and "
            "SYSTEM_EMAIL_PASSWORD. The sarah_c Gmail-API fallback was "
            "removed in Stage 2a."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_address
    msg["To"]      = to
    if body_plain:
        msg.attach(MIMEText(body_plain, "plain"))
    msg.attach(MIMEText(body_html if body_html else body_plain, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(from_address, to, msg.as_bytes())
    logger.info("System email sent via SMTP to %s, subject=%r", to, subject)
    return True


# ── Auth ─────────────────────────────────────────────────────────────────────

def _load_credentials(csm_id: str) -> Credentials:
    from product.auth_utils import load_google_credentials
    return load_google_credentials(csm_id)


# ── Email formatting ──────────────────────────────────────────────────────────

def _format_html(digest: dict) -> str:
    """
    Format the digest as a clean HTML email.
    Simple layout — no heavy branding for pilot.
    """
    digest_text = digest.get("digest_text", "")
    greeting = digest.get("greeting", "Good morning.")
    # New prompt embeds greeting as first line — don't render it twice
    show_greeting = not digest_text.startswith("Good morning")
    action_items = digest.get("action_items", [])
    date_str = digest.get("date", "")

    # Convert digest_text newlines to paragraphs
    paragraphs = "".join(
        f"<p style='margin: 0 0 12px 0;'>{p.strip()}</p>"
        for p in digest_text.split("\n\n")
        if p.strip()
    )

    # Action items list
    action_html = ""
    if action_items:
        items = "".join(
            f"<li style='margin-bottom: 6px;'>"
            f"<strong>{item.get('account', '')}</strong> — {item.get('action', '')}"
            f"</li>"
            for item in action_items
        )
        action_html = f"""
        <div style='margin-top: 24px; padding-top: 16px; border-top: 1px solid #e5e7eb;'>
            <p style='margin: 0 0 8px 0; font-size: 12px; text-transform: uppercase;
                      letter-spacing: 0.05em; color: #6b7280;'>Action items</p>
            <ul style='margin: 0; padding-left: 20px;'>
                {items}
            </ul>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style='font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                 font-size: 15px; line-height: 1.6; color: #111827;
                 max-width: 600px; margin: 0 auto; padding: 32px 24px;'>

        <p style='font-size: 12px; color: #9ca3af; margin: 0 0 24px 0;'>{date_str}</p>

        {f"<p style='margin: 0 0 20px 0; font-size: 16px;'>{greeting}</p>" if show_greeting else ""}

        {paragraphs}

        {action_html}

        <div style='margin-top: 32px; padding-top: 16px; border-top: 1px solid #e5e7eb;
                    font-size: 12px; color: #9ca3af;'>
            Reply to this email with questions or feedback.
        </div>

    </body>
    </html>
    """


def _format_plain_text(digest: dict) -> str:
    """Plain text fallback."""
    digest_text = digest.get("digest_text", "")
    greeting = digest.get("greeting", "Good morning.")
    if digest_text.startswith("Good morning"):
        lines = [digest_text]
    else:
        lines = [greeting, "", digest_text]
    action_items = digest.get("action_items", [])
    if action_items:
        lines += ["", "Action items:"]
        for item in action_items:
            lines.append(f"  - {item.get('account', '')}: {item.get('action', '')}")
    lines += ["", "Reply to this email with questions or feedback."]
    return "\n".join(lines)


def _build_subject(digest: dict) -> str:
    date_str = digest.get("date", "")
    accounts = digest.get("accounts_mentioned", [])
    if accounts:
        top_account = accounts[0]
        return f"Your morning digest — {top_account} needs attention · {date_str}"
    return f"Your morning digest · {date_str}"


# ── Disk persistence ──────────────────────────────────────────────────────────

def save_digest_to_disk(digest: dict, csm_id: str) -> Path:
    """
    Save the morning digest to product/data/digests/{csm_id}/{date}.json.
    Returns the path written.
    """
    from product.csm_id_validation import safe_data_path
    date_str = digest.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    file_path = safe_data_path(DATA_DIR / "digests", csm_id, f"{date_str}.json")
    dest_dir = file_path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    body = digest.get("digest_text", "").strip()
    greeting = digest.get("greeting", "").rstrip()
    # New prompt embeds the greeting as the first line — don't prepend if already present
    if greeting and not body.startswith("Good morning"):
        full_text = f"{greeting}\n\n{body}"
    else:
        full_text = body

    payload = {
        "csm_id": csm_id,
        "date": date_str,
        "digest_text": full_text,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return file_path


# ── Main function ─────────────────────────────────────────────────────────────

def send_digest_email(
    digest: dict,
    to_email: str,
    csm_id: str,
    from_email: str = FROM_EMAIL,
) -> dict:
    """
    Send the morning digest as an email to the CSM.

    Args:
        digest:    Output of build_morning_digest() from builder.py
        to_email:  CSM's email address
        csm_id:    CSM identifier for OAuth token lookup
        from_email: Sender address (default: settings.FROM_EMAIL)

    Returns:
        {
          "status": "sent",
          "message_id": "...",
          "sent_at": "2026-03-24T08:00:00Z"
        }

    Raises:
        FileNotFoundError: if OAuth token is missing
        googleapiclient.errors.HttpError: if Gmail API call fails
    """
    creds = _load_credentials(csm_id)
    service = build("gmail", "v1", credentials=creds)

    # Build MIME message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = _build_subject(digest)
    msg["From"] = from_email
    msg["To"] = to_email

    msg.attach(MIMEText(_format_plain_text(digest), "plain"))
    msg.attach(MIMEText(_format_html(digest), "html"))

    # Encode and send
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    message_id = result.get("id", "")
    sent_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    logger.info("Digest sent to %s (message_id: %s)", to_email, message_id)

    return {
        "status": "sent",
        "message_id": message_id,
        "sent_at": sent_at,
    }


# ── Notification email (short, drives to /app) ───────────────────────────────

def send_notification_email(
    digest: dict,
    to_email: str,
    csm_id: str,
    app_url: str | None = None,
    from_email: str = FROM_EMAIL,
) -> dict:
    """
    Send a short notification email pointing the CSM to the app.
    Sent via send_system_email (SMTP with the SYSTEM_EMAIL_* credentials).
    """
    if app_url is None:
        from product.settings import public_url
        app_url = public_url("/app")
    greeting = digest.get("greeting", "Good morning.")
    signal_count = len(digest.get("action_items", [])) or len(digest.get("accounts_mentioned", []))
    account_count = len(digest.get("accounts_mentioned", []))
    date_str = digest.get("date", "")

    subject = "CS Agent — Your daily briefing is ready"

    plain = (
        f"{greeting}\n\n"
        f"Your daily briefing is ready with {signal_count} signals across {account_count} accounts.\n\n"
        f"Open CS Agent → {app_url}\n\n"
        f"—\n"
        f"Reply STOP to turn off email notifications."
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style='font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
             font-size: 15px; line-height: 1.6; color: #111827;
             max-width: 600px; margin: 0 auto; padding: 32px 24px;'>

    <p style='font-size: 12px; color: #9ca3af; margin: 0 0 24px 0;'>{date_str}</p>

    <p style='margin: 0 0 16px 0; font-size: 16px;'>{greeting}</p>

    <p style='margin: 0 0 20px 0;'>
        Your daily briefing is ready with <strong>{signal_count} signals</strong>
        across <strong>{account_count} accounts</strong>.
    </p>

    <p style='margin: 0 0 32px 0;'>
        <a href='{app_url}'
           style='display: inline-block; background: #2C5F8A; color: #fff;
                  padding: 10px 20px; border-radius: 6px; text-decoration: none;
                  font-weight: 500; font-size: 14px;'>
            Open CS Agent &rarr;
        </a>
    </p>

    <div style='padding-top: 16px; border-top: 1px solid #e5e7eb;
                font-size: 12px; color: #9ca3af;'>
        Reply STOP to turn off email notifications.
    </div>

</body>
</html>"""

    if "@noreply.clerk" in to_email:
        logger.info("Skipping notification, recipient is noreply fallback: %s", to_email)
        return {"status": "skipped", "reason": "noreply_fallback"}

    delivered = send_system_email(to=to_email, subject=subject, body_html=html, body_plain=plain)

    if not delivered:
        # send_system_email also pre-filters @noreply.clerk; the wrapper guard
        # above already covers it, so this branch is defense in depth for any
        # future skip reasons added inside send_system_email.
        return {"status": "skipped", "reason": "recipient_skipped"}

    sent_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    logger.info("Notification email sent to %s", to_email)

    return {"status": "sent", "message_id": "smtp", "sent_at": sent_at}


# ── Phase 3.3 — Proactive alert delivery ─────────────────────────────────────

def send_proactive_alert(
    signal: dict,
    urgency_score: float,
    csm_id: str,
    csm_email: str,
    suggested_draft: str,
    *,
    signal_id: str,
    send_email: bool = True,
) -> dict:
    """
    Record a proactive alert for the CSM when urgency score exceeds threshold.

    Args:
        signal:         Absence signal dict from detect_absence_signals()
        urgency_score:  Float from score_urgency() — must be >= 0.65 to record
        csm_id:         CSM identifier for OAuth token lookup
        csm_email:      CSM email address to send alert to (only used when send_email=True)
        suggested_draft: LLM-generated draft email to the contact (not sent automatically)
        signal_id:      Stable dedup key derived from the signal (Phase 5 — Track G).
                        Use product.signal_cache.derive_gmail_signal_id(signal).
                        The (csm_id, account, signal_id, alert_date) tuple is the
                        partial UNIQUE index target — duplicate inserts on the same
                        day are caught at the DB layer before any email is sent.
        send_email:     When True (default), load Google creds, build the Gmail
                        service, and send the alert email after the DB insert.
                        When False, the path is IN-APP ONLY: the ProactiveAlert
                        row and the disk artifact are still written, but NO Google
                        credentials are loaded and NO email is sent. This makes the
                        in-app surface independent of the CSM's Google token state.

    Returns:
        {"status": "sent", "message_id": "...", "sent_at": "..."}   (send_email=True)
        {"status": "in_app", "message_id": "", "sent_at": "..."}    (send_email=False)
        {"status": "skipped", "reason": "urgency_below_threshold", "score": ...}
        {"status": "duplicate_suppressed", "signal_id": "..."}

    CRITICAL: The suggested_draft is a SUGGESTION only.
    CS Agent does not send it. The CSM must explicitly approve.
    This is the human-in-the-loop principle.
    """
    URGENCY_THRESHOLD = 0.65

    if urgency_score < URGENCY_THRESHOLD:
        return {
            "status": "skipped",
            "reason": "urgency_below_threshold",
            "score": urgency_score,
        }

    account = signal.get("account", "Unknown Account")
    contact = signal.get("contact", "your contact")
    detail = signal.get("detail", "")
    renewal_days = signal.get("renewal_days")

    # ── Subject ───────────────────────────────────────────────────────────────
    subject = f"⚠ {account} needs attention"
    if renewal_days and renewal_days < 90:
        subject += f" — renews in {renewal_days} days"

    # ── HTML body ─────────────────────────────────────────────────────────────
    renewal_note = (
        f"<p style='margin: 0 0 8px 0; color: #dc2626;'>"
        f"<strong>Renewal in {renewal_days} days.</strong></p>"
        if renewal_days and renewal_days < 90 else ""
    )

    draft_section = f"""
    <div style='margin-top: 24px; padding: 16px; background: #f9fafb;
                border: 1px solid #e5e7eb; border-radius: 6px;'>
        <p style='margin: 0 0 8px 0; font-size: 12px; text-transform: uppercase;
                  letter-spacing: 0.05em; color: #6b7280;'>Suggested draft — review before sending</p>
        <p style='margin: 0; white-space: pre-wrap; font-size: 14px; color: #374151;'>{suggested_draft}</p>
    </div>
    <p style='margin: 16px 0 0 0; font-size: 12px; color: #9ca3af;'>
        This draft is a suggestion. CS Agent has not sent anything.
        Review, edit if needed, then send from your Gmail.
    </p>
    """ if suggested_draft else ""

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style='font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                 font-size: 15px; line-height: 1.6; color: #111827;
                 max-width: 600px; margin: 0 auto; padding: 32px 24px;'>

        <p style='font-size: 12px; color: #9ca3af; margin: 0 0 24px 0;'>
            CS Agent · Proactive Alert · Urgency score: {urgency_score:.2f}
        </p>

        <p style='margin: 0 0 16px 0; font-size: 16px; font-weight: 600;'>
            {account} needs your attention.
        </p>

        {renewal_note}

        <p style='margin: 0 0 16px 0;'>{detail}</p>

        {draft_section}

        <div style='margin-top: 32px; padding-top: 16px; border-top: 1px solid #e5e7eb;
                    font-size: 12px; color: #9ca3af;'>
            Reply to this email with questions or feedback.
        </div>

    </body>
    </html>
    """

    plain_body = f"CS Agent — Proactive Alert\n\n{account} needs your attention.\n\n{detail}\n"
    if renewal_days and renewal_days < 90:
        plain_body += f"Renewal in {renewal_days} days.\n"
    if suggested_draft:
        plain_body += f"\nSuggested draft (review before sending):\n\n{suggested_draft}\n"
    plain_body += "\nThis draft is a suggestion. CS Agent has not sent anything."

    # ── Insert ProactiveAlert BEFORE any send (Phase 5 — Track G) ─────────────
    # The partial UNIQUE index on (csm_id, account, signal_id, alert_date)
    # is the source of truth for "has this alert been recorded today." If the
    # insert raises IntegrityError, a row already exists for this key —
    # suppress the duplicate. event_id is generated here so the same UUID
    # flows through both the DB row and the downstream disk artifact.
    event_id = str(uuid.uuid4())
    alert_date = datetime.now(timezone.utc).date()
    from product.database import SessionLocal, ProactiveAlert
    from sqlalchemy.exc import IntegrityError
    if SessionLocal:
        _alert_db = SessionLocal()
        try:
            _alert_db.add(ProactiveAlert(
                id=event_id,
                csm_id=csm_id,
                account=account,
                alert_text=f"{account} needs your attention.\n\n{detail}",
                draft_text=suggested_draft,
                urgency=f"{urgency_score:.2f}",
                signal_id=signal_id,
                alert_date=alert_date,
            ))
            _alert_db.commit()
        except IntegrityError:
            _alert_db.rollback()
            logger.info(json.dumps({
                "event": "f1_duplicate_alert_suppressed",
                "csm_id": csm_id,
                "account": account,
                "signal_id": signal_id,
            }))
            _alert_db.close()
            return {"status": "duplicate_suppressed", "signal_id": signal_id}
        finally:
            _alert_db.close()

    # ── Send the email (only when send_email) ─────────────────────────────────
    # In-app-only mode (send_email=False) loads NO Google credentials and
    # builds NO Gmail service — the row above plus the disk artifact below are
    # the full in-app surface, independent of the CSM's Google token state.
    message_id = ""
    if send_email:
        creds = _load_credentials(csm_id)
        service = build("gmail", "v1", credentials=creds)

        # Build MIME message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL
        msg["To"] = csm_email
        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        result = service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()
        message_id = result.get("id", "")

    sent_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Persist alert to disk for in-app polling (/app/alerts). Written in both
    # modes — in-app polling depends on it regardless of whether email was sent.
    try:
        alert_dir = DATA_DIR / "alerts" / csm_id
        alert_dir.mkdir(parents=True, exist_ok=True)
        safe_ts = sent_at.replace(":", "-").replace("+", "-")
        alert_payload = {
            "alert_id": event_id,
            "timestamp": sent_at,
            "text": f"{account} needs your attention.\n\n{detail}",
            "draft": suggested_draft,
        }
        (alert_dir / f"{safe_ts}.json").write_text(
            json.dumps(alert_payload, indent=2), encoding="utf-8"
        )
    except Exception as _e:
        logger.warning("could not save alert to disk: %s", _e)

    if send_email:
        logger.info("Proactive alert sent to %s for %s (score: %s)", csm_email, account, urgency_score)
        return {"status": "sent", "message_id": message_id, "sent_at": sent_at}

    logger.info("Proactive alert recorded in-app for %s (score: %s)", account, urgency_score)
    return {"status": "in_app", "message_id": "", "sent_at": sent_at}
