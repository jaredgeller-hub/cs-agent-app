"""
gmail_helper.py

Single public function for sending Gmail on a CSM's behalf. Uses the
PLG-ready Postgres-first credential loader (product.auth_utils) so it
works for Clerk users and sarah_c alike. DEMO_MODE has been removed;
main runs PLG-only, demo lives on the demo-frozen branch.

The only caller on main today is the test suite. Commit 6 of A.1 wires
this function into /app/actions/execute for the "approve + send" path.

Threading: callers pass thread_id when they have one (e.g., replies to
a signal-extracted inbound email). When present, the function fetches
the thread's most recent Message-Id from Gmail and populates
In-Reply-To + References MIME headers plus threadId in the send body.
When absent, a fresh message is sent — no silent fallback if threading
metadata is malformed.

Exception contract:
  - GmailScopeRevokedError: Google access unavailable in a way that
    requires the CSM to reconnect (missing creds, 401, 403). Commit 6
    catches this to flip google_actions_enabled=False and surface the
    reconnect UI.
  - Everything else bubbles (5xx, network errors, 404 on stale
    thread_id, MIME encoding failures). No silent successes, no
    log-and-swallow.
"""

from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from product.auth_utils import load_google_credentials

logger = logging.getLogger("cs-agent.gmail_helper")


class GmailScopeRevokedError(Exception):
    """Raised when send cannot proceed because the CSM's Google grant
    is missing, expired, or lacks gmail.send scope. Caller (Commit 6
    of A.1) catches this to flip google_actions_enabled=False and surface
    the reconnect UI.

    Covers three distinct failure modes that share identical remediation:
      1. No stored credentials (FileNotFoundError from load_google_credentials)
      2. Gmail API HttpError 401 (token invalid / revoked)
      3. Gmail API HttpError 403 (scope insufficient)
    """


def send_gmail_message(
    csm_id: str,
    to: str,
    subject: str,
    body_text: str,
    thread_id: str | None = None,
    from_name: str | None = None,
) -> str:
    """Send an email via the Gmail API on the given CSM's behalf.

    Args:
        csm_id: Clerk user ID or legacy sarah_c identity. Credentials
            are loaded via product.auth_utils.load_google_credentials
            (Postgres first, filesystem fallback, refresh under lock).
        to: RFC 5322 recipient address (e.g. "foo@example.com").
        subject: Subject line; supports non-ASCII (MIMEText handles
            RFC 2047 encoding).
        body_text: Plain-text body; Unicode safe.
        thread_id: Gmail thread id (not Message-Id). When provided,
            the send is threaded: In-Reply-To + References headers
            set to the last message's RFC 822 Message-Id, and
            threadId set in the Gmail API send body.
        from_name: Optional display name for the From header. The
            email address is not overridden (Gmail uses the
            authenticated account's address).

    Returns:
        The Gmail API message id of the sent message (str).

    Raises:
        GmailScopeRevokedError: Credentials missing / 401 / 403.
        HttpError: Other Gmail API failures (5xx, 404 on stale
            thread_id, etc.). Bubbles unchanged.
        Exception: Other unexpected errors. Bubble unchanged — no
            silent fallback to simulated send.
    """
    try:
        creds = load_google_credentials(csm_id)
    except FileNotFoundError as exc:
        raise GmailScopeRevokedError(
            f"No Google credentials for {csm_id}; user must reconnect"
        ) from exc

    service = build("gmail", "v1", credentials=creds)

    # Build MIME. MIMEText handles unicode in body via utf-8 charset;
    # Subject is encoded via RFC 2047 by MIMEText.__setitem__.
    mime = MIMEText(body_text, _charset="utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    if from_name:
        mime["From"] = from_name  # address inferred by Gmail from auth'd account

    # Threading: fetch the latest Message-Id on the thread and wire
    # In-Reply-To + References. Gmail also requires threadId in the
    # send body to associate the new message with the thread.
    send_body: dict = {}
    if thread_id:
        thread = (
            service.users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["Message-Id"],
            )
            .execute()
        )
        messages = thread.get("messages", [])
        last_message_id = None
        if messages:
            for header in messages[-1].get("payload", {}).get("headers", []):
                # Gmail sometimes returns "Message-Id" vs "Message-ID"; match
                # case-insensitively.
                if header.get("name", "").lower() == "message-id":
                    last_message_id = header.get("value")
                    break
        if last_message_id:
            mime["In-Reply-To"] = last_message_id
            mime["References"] = last_message_id
        send_body["threadId"] = thread_id

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    send_body["raw"] = raw

    try:
        response = (
            service.users().messages().send(userId="me", body=send_body).execute()
        )
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (401, 403):
            raise GmailScopeRevokedError(
                f"Gmail API refused send for {csm_id}: HTTP {status}"
            ) from exc
        raise

    message_id = response.get("id")
    if not message_id:
        raise RuntimeError(
            f"Gmail API returned no message id for {csm_id}: {response!r}"
        )
    logger.info("Gmail send OK: csm_id=%s gmail_message_id=%s thread=%s",
                csm_id, message_id, thread_id or "<new>")
    return message_id
