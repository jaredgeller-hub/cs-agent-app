"""
product/signals/gmail.py

Gmail signal extraction for the product pipeline.
Self-contained — reads the OAuth token directly and calls the Gmail API.
Replaces gmail_helper.py for all product use cases.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger("cs-agent.gmail")


def _load_credentials(csm_id: str) -> Credentials:
    from product.auth_utils import load_google_credentials
    return load_google_credentials(csm_id)


def _fetch_threads(service, query: str, days: int = 14) -> list[dict]:
    """
    Fetch threads matching a Gmail search query from the last `days` days.
    Requests metadata format only — message body is never fetched.
    Returns processed thread dicts including raw messages list for signal computation.
    """
    now = datetime.now(timezone.utc)
    after_date = (now - timedelta(days=days)).strftime("%Y/%m/%d")
    full_query = f"({query}) after:{after_date}"

    # Paginate through thread results (up to 500 threads)
    all_items = []
    page_token = None
    max_pages = 5  # 5 pages x 100 = 500 threads max

    for _ in range(max_pages):
        try:
            response = service.users().threads().list(
                userId="me", q=full_query, maxResults=100, pageToken=page_token,
            ).execute()
        except Exception as e:
            logger.warning("threads.list error: %s", e)
            break

        all_items.extend(response.get("threads", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    results = []
    for item in all_items:
        try:
            thread = service.users().threads().get(
                userId="me",
                id=item["id"],
                format="metadata",
                metadataHeaders=["Subject", "From", "To", "Date"],
            ).execute()
        except Exception as e:
            logger.warning("thread.get error %s: %s", item['id'], e)
            continue

        messages = thread.get("messages", [])
        if not messages:
            continue

        first_headers = {
            h["name"]: h["value"]
            for h in messages[0].get("payload", {}).get("headers", [])
        }
        last_msg = messages[-1]
        last_headers = {
            h["name"]: h["value"]
            for h in last_msg.get("payload", {}).get("headers", [])
        }
        last_date = datetime.fromtimestamp(
            int(last_msg.get("internalDate", 0)) / 1000, tz=timezone.utc
        )

        results.append({
            "thread_id": item["id"],
            "subject": first_headers.get("Subject", "(no subject)"),
            "last_from": last_headers.get("From", ""),
            "date": last_date.isoformat(),
            "messages": messages,  # in-memory only, metadata headers + internalDate, no body
        })

    return results


def _compute_thread_signals(
    thread: dict, contact_email: str, now: datetime
) -> tuple[str, bool, int | None]:
    """
    Derive direction, got_reply, and days_since_last_inbound for a thread
    relative to a specific contact email.

    Returns:
        direction: "sent" | "received" (from CSM's perspective)
        got_reply: True if contact sent a message after the CSM's last outbound
        days_since_last_inbound: days since contact's most recent message, or None
    """
    messages = thread.get("messages", [])
    contact_lower = contact_email.lower()

    # direction: who sent the last message?
    last_from = thread.get("last_from", "").lower()
    direction = "received" if contact_lower in last_from else "sent"

    # Walk messages in reverse to find contact's most recent message
    last_inbound_date = None
    days_since_last_inbound = None
    for msg in reversed(messages):
        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        if contact_lower in headers.get("From", "").lower():
            last_inbound_date = datetime.fromtimestamp(
                int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc
            )
            days_since_last_inbound = (now - last_inbound_date).days
            break

    # got_reply: did the contact respond after the CSM's last outbound?
    last_outbound_date = None
    for msg in messages:
        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        if contact_lower not in headers.get("From", "").lower():
            last_outbound_date = datetime.fromtimestamp(
                int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc
            )

    got_reply = bool(
        last_outbound_date and last_inbound_date and last_inbound_date > last_outbound_date
    )

    return direction, got_reply, days_since_last_inbound


def extract_gmail_signals(csm_id: str, account_contacts: dict) -> list[dict]:
    """
    Connect to the CSM's Gmail and extract relationship signals.

    Args:
        csm_id: e.g. "sarah_c" — must have a token at tokens/{csm_id}.json
        account_contacts: {
            "Northgate Logistics": ["dana.mills@northgate.example"],
            "Beacon Financial": ["priya.sharma@beaconfinancial.example"],
            ...
        }

    Returns:
        List of signal dicts. Signal types:
        - "email.absence"         — CSM sent last, no reply in >3 days
        - "email.received"        — Contact sent the most recent message
        - "email.new_stakeholder" — Thread from unrecognized contact at a known account's domain

    Architectural constraint: only Gmail metadata headers are fetched (format="metadata").
    Raw message body is never requested, read, or stored.
    """
    creds = _load_credentials(csm_id)
    service = build("gmail", "v1", credentials=creds)

    signals = []
    now = datetime.now(timezone.utc)

    for account, contacts in account_contacts.items():
        known_thread_ids = set()

        # ── Pass 1: per-contact sweep ─────────────────────────────────────
        # One query per known contact email. Identifies absence and active threads.
        for contact_email in contacts:
            threads = _fetch_threads(
                service,
                query=f"from:{contact_email} OR to:{contact_email}",
                days=14,
            )

            for thread in threads:
                known_thread_ids.add(thread["thread_id"])
                subject = thread["subject"]
                timestamp = thread["date"]

                direction, got_reply, days_waiting = _compute_thread_signals(
                    thread, contact_email, now
                )

                if direction == "sent" and not got_reply:
                    if days_waiting is None:
                        try:
                            last_dt = datetime.fromisoformat(
                                timestamp.replace("Z", "+00:00")
                            )
                            days_waiting = (now - last_dt).days
                        except Exception:
                            days_waiting = 0

                    if days_waiting > 3:
                        signals.append({
                            "signal_type": "email.absence",
                            "account": account,
                            "contact": contact_email,
                            "detail": f"Last outreach sent {days_waiting} days ago, no reply",
                            "days_waiting": days_waiting,
                            "thread_subject": subject,
                            "timestamp": timestamp,
                            "thread_id": thread["thread_id"],
                        })

                elif direction == "received":
                    signals.append({
                        "signal_type": "email.received",
                        "account": account,
                        "contact": contact_email,
                        "detail": f"Inbound message in thread '{subject}'",
                        "days_waiting": None,
                        "thread_subject": subject,
                        "timestamp": timestamp,
                        "thread_id": thread["thread_id"],
                    })

        # ── Pass 2: domain sweep for new stakeholders ─────────────────────
        # Queries each account's email domain. Thread IDs not seen in Pass 1
        # indicate a contact not in the known list — a new stakeholder signal.
        domains = {c.split("@")[1] for c in contacts if "@" in c}
        for domain in domains:
            domain_threads = _fetch_threads(
                service,
                query=f"from:@{domain} OR to:@{domain}",
                days=14,
            )
            for thread in domain_threads:
                if thread["thread_id"] in known_thread_ids:
                    continue
                signals.append({
                    "signal_type": "email.new_stakeholder",
                    "account": account,
                    "contact": f"unknown@{domain}",
                    "detail": f"Thread with unrecognized contact at {domain}",
                    "days_waiting": None,
                    "thread_subject": thread["subject"],
                    "timestamp": thread["date"],
                    "thread_id": thread["thread_id"],
                })

    return signals


def extract_threads_for_contact(csm_id: str, contact_email: str, days: int = 14) -> list[dict]:
    """Fetch recent Gmail threads to/from a contact email. Returns metadata only.

    Wraps _fetch_threads with a contact-email-scoped query, then runs
    _compute_thread_signals per thread to derive direction, got_reply, and
    days_since_last_inbound. Quoted query handles emails with dots and
    plus-aliases.

    Returns a list of thread dicts with thread_id, subject, last_from,
    last_date, message_count, direction, got_reply, days_since_last_inbound.
    The raw messages list is intentionally dropped from the output.

    Raises the same exceptions _fetch_threads / _load_credentials can raise
    (auth errors, API errors). Caller handles.
    """
    if not contact_email or "@" not in contact_email:
        return []

    creds = _load_credentials(csm_id)
    service = build("gmail", "v1", credentials=creds)

    query = f'from:"{contact_email}" OR to:"{contact_email}"'
    threads = _fetch_threads(service, query, days=days)

    now = datetime.now(timezone.utc)
    result = []
    for thread in threads:
        direction, got_reply, days_since = _compute_thread_signals(thread, contact_email, now)
        result.append({
            "thread_id": thread.get("thread_id", ""),
            "subject": thread.get("subject", ""),
            "last_from": thread.get("last_from", ""),
            "last_date": thread.get("date", ""),
            "message_count": len(thread.get("messages", [])),
            "direction": direction,
            "got_reply": got_reply,
            "days_since_last_inbound": days_since,
        })

    return result
