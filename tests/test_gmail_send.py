"""
tests/test_gmail_send.py

Regression guards for gmail_helper.send_gmail_message — the single
public send path rewritten in A.1 Commit 3. All tests mock
gmail_helper.load_google_credentials and gmail_helper.build (the
discovery.build reference imported at gmail_helper module scope) so
no network calls and no credential lookups touch the real
Postgres/filesystem paths.

Invariants under test:
  1. Happy path — returns Gmail API message id, calls send with
     userId='me' and a body containing 'raw'.
  2. No thread_id → no In-Reply-To / References / threadId.
  3. thread_id → threads().get() fetches last Message-Id; In-Reply-To,
     References, threadId all set correctly.
  4. 401 from Gmail API → GmailScopeRevokedError.
  5. 403 from Gmail API → GmailScopeRevokedError.
  6. FileNotFoundError from load_google_credentials → GmailScopeRevokedError.
  7. Unicode round-trip through MIME — subject + body with emoji and
     non-ASCII characters decode back cleanly.

Standalone-runnable:
    python3 tests/test_gmail_send.py
"""

import base64
import email
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from googleapiclient.errors import HttpError

import gmail_helper
from gmail_helper import GmailScopeRevokedError, send_gmail_message


# ── Mock builders ────────────────────────────────────────────────────────────

def _build_fake_service(send_response=None, send_exception=None, threads_response=None):
    """Construct a MagicMock service whose method chains match the Gmail
    API surface we call:
        service.users().messages().send(userId=..., body=...).execute()
        service.users().threads().get(userId=..., id=..., format=...,
                                       metadataHeaders=...).execute()

    send_response    — dict returned by .send(...).execute()
    send_exception   — exception raised by .send(...).execute() instead
    threads_response — dict returned by .threads().get(...).execute()
    """
    service = MagicMock(name="gmail_service")

    send_exec = MagicMock(name="send_execute")
    if send_exception is not None:
        send_exec.execute.side_effect = send_exception
    else:
        send_exec.execute.return_value = send_response or {"id": "msg-default"}
    service.users.return_value.messages.return_value.send.return_value = send_exec

    threads_exec = MagicMock(name="threads_execute")
    threads_exec.execute.return_value = threads_response or {"messages": []}
    service.users.return_value.threads.return_value.get.return_value = threads_exec

    return service


def _http_error(status: int) -> HttpError:
    """Build a minimal HttpError with the given resp.status."""
    resp = MagicMock()
    resp.status = status
    resp.reason = "Test"
    return HttpError(resp, b"{}", uri="https://gmail.googleapis.com/test")


def _extract_sent_body(service_mock) -> dict:
    """Pull out the body= kwarg from the most recent service.send(...) call."""
    send_mock = service_mock.users.return_value.messages.return_value.send
    _, kwargs = send_mock.call_args
    return kwargs["body"]


def _decode_raw(raw: str):
    """Decode the base64url-encoded MIME back into an email.message.Message."""
    raw_bytes = base64.urlsafe_b64decode(raw.encode("ascii"))
    return email.message_from_bytes(raw_bytes)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_send_succeeds_with_valid_credentials():
    """Happy path: returns the message id; calls send with userId='me'
    and body containing 'raw'.
    """
    fake_creds = MagicMock(name="credentials")
    fake_service = _build_fake_service(send_response={"id": "msg-abc"})

    with patch.object(gmail_helper, "load_google_credentials", return_value=fake_creds), \
         patch.object(gmail_helper, "build", return_value=fake_service) as build_mock:
        result = send_gmail_message(
            csm_id="user_happy",
            to="recipient@example.com",
            subject="Hello",
            body_text="Body",
        )

    assert result == "msg-abc"
    build_mock.assert_called_once_with("gmail", "v1", credentials=fake_creds)

    send_mock = fake_service.users.return_value.messages.return_value.send
    send_mock.assert_called_once()
    _, kwargs = send_mock.call_args
    assert kwargs["userId"] == "me"
    assert "raw" in kwargs["body"]
    print("✓ happy path: returns msg id; send called with userId='me' and body['raw']")


def test_send_without_thread_id_omits_threading():
    """No thread_id → no In-Reply-To, no References, no threadId in body.
    Threads().get() must not be called."""
    fake_creds = MagicMock()
    fake_service = _build_fake_service(send_response={"id": "msg-nothreaded"})

    with patch.object(gmail_helper, "load_google_credentials", return_value=fake_creds), \
         patch.object(gmail_helper, "build", return_value=fake_service):
        send_gmail_message(
            csm_id="user_nothread",
            to="to@example.com",
            subject="s",
            body_text="b",
        )

    body = _extract_sent_body(fake_service)
    assert "threadId" not in body, f"threadId leaked into non-threaded send: {body!r}"

    mime = _decode_raw(body["raw"])
    assert mime.get("In-Reply-To") is None
    assert mime.get("References") is None

    threads_get = fake_service.users.return_value.threads.return_value.get
    threads_get.assert_not_called()
    print("✓ no thread_id: no threading headers, no threadId, threads().get() unused")


def test_send_with_thread_id_sets_threadId_and_headers():
    """thread_id → fetch last Message-Id via threads().get(); set both
    In-Reply-To and References to that Message-Id; set threadId in body.
    """
    fake_creds = MagicMock()
    thread_payload = {
        "messages": [
            {"payload": {"headers": [{"name": "Message-Id", "value": "<earlier@example.com>"}]}},
            {"payload": {"headers": [{"name": "Message-Id", "value": "<orig@example.com>"}]}},
        ],
    }
    fake_service = _build_fake_service(
        send_response={"id": "msg-threaded"},
        threads_response=thread_payload,
    )

    with patch.object(gmail_helper, "load_google_credentials", return_value=fake_creds), \
         patch.object(gmail_helper, "build", return_value=fake_service):
        send_gmail_message(
            csm_id="user_threaded",
            to="to@example.com",
            subject="Re: thing",
            body_text="body",
            thread_id="thr-123",
        )

    # threads.get called with the right args
    threads_get = fake_service.users.return_value.threads.return_value.get
    threads_get.assert_called_once_with(
        userId="me",
        id="thr-123",
        format="metadata",
        metadataHeaders=["Message-Id"],
    )

    body = _extract_sent_body(fake_service)
    assert body.get("threadId") == "thr-123"

    mime = _decode_raw(body["raw"])
    assert mime.get("In-Reply-To") == "<orig@example.com>"
    assert mime.get("References") == "<orig@example.com>"
    print("✓ threaded: threadId + In-Reply-To + References all set from latest Message-Id")


def test_send_raises_scope_revoked_on_401():
    """HTTP 401 from Gmail API → GmailScopeRevokedError (user must reconnect)."""
    fake_creds = MagicMock()
    fake_service = _build_fake_service(send_exception=_http_error(401))

    with patch.object(gmail_helper, "load_google_credentials", return_value=fake_creds), \
         patch.object(gmail_helper, "build", return_value=fake_service):
        with pytest.raises(GmailScopeRevokedError):
            send_gmail_message(
                csm_id="user_401",
                to="to@example.com",
                subject="s",
                body_text="b",
            )
    print("✓ 401 → GmailScopeRevokedError")


def test_send_raises_scope_revoked_on_403():
    """HTTP 403 from Gmail API → GmailScopeRevokedError (scope insufficient)."""
    fake_creds = MagicMock()
    fake_service = _build_fake_service(send_exception=_http_error(403))

    with patch.object(gmail_helper, "load_google_credentials", return_value=fake_creds), \
         patch.object(gmail_helper, "build", return_value=fake_service):
        with pytest.raises(GmailScopeRevokedError):
            send_gmail_message(
                csm_id="user_403",
                to="to@example.com",
                subject="s",
                body_text="b",
            )
    print("✓ 403 → GmailScopeRevokedError")


def test_send_raises_scope_revoked_when_no_credentials():
    """FileNotFoundError from load_google_credentials → GmailScopeRevokedError.
    User never connected Google OR token row was deleted.
    """
    def _raises(*_args, **_kwargs):
        raise FileNotFoundError("no creds for user_nocreds")

    with patch.object(gmail_helper, "load_google_credentials", side_effect=_raises):
        with pytest.raises(GmailScopeRevokedError):
            send_gmail_message(
                csm_id="user_nocreds",
                to="to@example.com",
                subject="s",
                body_text="b",
            )
    print("✓ FileNotFoundError → GmailScopeRevokedError")


def test_mime_roundtrips_unicode():
    """Subject with non-ASCII + body with emoji survive MIME round-trip."""
    fake_creds = MagicMock()
    fake_service = _build_fake_service(send_response={"id": "msg-unicode"})

    subject = "Résumé 你好"
    body = "Hello 👋 — emoji check, ü, and 日本語."

    with patch.object(gmail_helper, "load_google_credentials", return_value=fake_creds), \
         patch.object(gmail_helper, "build", return_value=fake_service):
        send_gmail_message(
            csm_id="user_unicode",
            to="to@example.com",
            subject=subject,
            body_text=body,
        )

    body_sent = _extract_sent_body(fake_service)
    mime = _decode_raw(body_sent["raw"])

    # Subject gets RFC 2047 encoded on the wire; decode_header() reverses it
    decoded_parts = email.header.decode_header(mime["Subject"])
    decoded_subject = "".join(
        (part.decode(enc or "utf-8") if isinstance(part, bytes) else part)
        for part, enc in decoded_parts
    )
    assert decoded_subject == subject, f"subject round-trip failed: got {decoded_subject!r}"

    payload = mime.get_payload(decode=True).decode(mime.get_content_charset() or "utf-8")
    assert payload == body, f"body round-trip failed: got {payload!r}"
    print("✓ unicode MIME round-trips: Subject + body both decode cleanly")


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
