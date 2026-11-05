"""
product/signals/calendar.py

Calendar signal extraction for the product pipeline.
Self-contained — reads the OAuth token directly and calls the Google Calendar API.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger("cs-agent.calendar")


def _load_credentials(csm_id: str) -> Credentials:
    from product.auth_utils import load_google_credentials
    return load_google_credentials(csm_id)


def _get_csm_tz_name(csm_id: str) -> str:
    """Return the IANA timezone string for a CSM, defaulting to America/Chicago."""
    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return "America/Chicago"
        session = SessionLocal()
        try:
            cust = session.query(Customer).filter_by(clerk_user_id=csm_id).first()
            if cust and cust.timezone:
                return cust.timezone
        finally:
            session.close()
    except Exception:
        pass
    return "America/Chicago"


def _parse_event_time(time_obj: dict) -> datetime | None:
    """Parse a Google Calendar event time object to a timezone-aware datetime."""
    if "dateTime" in time_obj:
        dt = datetime.fromisoformat(time_obj["dateTime"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if "date" in time_obj:
        # All-day event — treat as midnight UTC
        d = date.fromisoformat(time_obj["date"])
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return None


def _classify_event(event: dict, account_names: list[str]) -> str:
    """
    Classify a calendar event as "customer", "internal", or "personal".

    - customer: an account name appears in the title or attendee names/emails
    - internal: has attendees but no account name match
    - personal: no attendees or a single-person block
    """
    title = (event.get("summary") or "").lower()
    attendees = event.get("attendees", [])

    for account in account_names:
        if account.lower() in title:
            return "customer"

    for attendee in attendees:
        display = (attendee.get("displayName") or "").lower()
        email = (attendee.get("email") or "").lower()
        for account in account_names:
            acct_lower = account.lower()
            if acct_lower in display or acct_lower in email:
                return "customer"
            # Loose domain hint: first word of account name (≥5 chars) in email
            first_word = acct_lower.split()[0]
            if len(first_word) >= 5 and first_word in email:
                return "customer"

    if attendees:
        return "internal"
    return "personal"


def _match_account(event: dict, account_names: list[str]) -> str:
    """Return the account name matched to this event, or 'Unknown Account'."""
    title_lower = (event.get("summary") or "").lower()
    for account in account_names:
        if account.lower() in title_lower:
            return account

    for account in account_names:
        acct_lower = account.lower()
        for attendee in event.get("attendees", []):
            display = (attendee.get("displayName") or "").lower()
            email = (attendee.get("email") or "").lower()
            if acct_lower in display or acct_lower in email:
                return account

    return "Unknown Account"


def _extract_attendee_names(event: dict) -> list[str]:
    """Return display names (or emails) of non-self attendees."""
    names = []
    for attendee in event.get("attendees", []):
        if attendee.get("self"):
            continue
        name = attendee.get("displayName") or attendee.get("email", "")
        if name:
            names.append(name)
    return names


def _has_prep_block(event_start: datetime, all_events: list[dict]) -> bool:
    """
    Return True if any event ending within 30 minutes before event_start
    has a prep/focus/review keyword in its title.
    """
    window_open = event_start - timedelta(minutes=30)
    for ev in all_events:
        end_dt = _parse_event_time(ev.get("end", {}))
        if end_dt and window_open <= end_dt <= event_start:
            title = (ev.get("summary") or "").lower()
            if any(kw in title for kw in ("prep", "focus", "review", "research")):
                return True
    return False


def _compute_available_gaps(
    events_today: list[dict],
    day_start_hour: int = 9,
    day_end_hour: int = 17,
    tz_name: str = "America/Chicago",
) -> list[str]:
    """
    Return free time ranges during business hours as "HH:MM-HH:MM" strings.
    Merges overlapping events before computing gaps.

    The day anchor is resolved using datetime.now(ZoneInfo(tz_name)).date()
    so the day window aligns with the CSM's local "today" rather than UTC
    today. Without this, a 9pm Pacific caller would compute gaps against
    UTC-tomorrow's date, anchored at next-day's 9am.
    """
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    day_start = datetime(today.year, today.month, today.day, day_start_hour, 0, tzinfo=tz)
    day_end = datetime(today.year, today.month, today.day, day_end_hour, 0, tzinfo=tz)

    # Collect busy intervals clipped to business hours
    busy = []
    for ev in events_today:
        start = _parse_event_time(ev.get("start", {}))
        end = _parse_event_time(ev.get("end", {}))
        if not start or not end:
            continue
        s = max(start, day_start)
        e = min(end, day_end)
        if s < e:
            busy.append((s, e))

    if not busy:
        return [f"{day_start_hour:02d}:00-{day_end_hour:02d}:00"]

    # Sort and merge overlapping intervals
    busy.sort(key=lambda x: x[0])
    merged = [list(busy[0])]
    for s, e in busy[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # Gaps between merged intervals
    gaps = []
    cursor = day_start
    for s, e in merged:
        if cursor < s:
            gaps.append(f"{cursor.strftime('%H:%M')}-{s.strftime('%H:%M')}")
        cursor = e
    if cursor < day_end:
        gaps.append(f"{cursor.strftime('%H:%M')}-{day_end.strftime('%H:%M')}")

    return gaps


def _event_bucket_date(event: dict, tz: ZoneInfo) -> date | None:
    """Return the date this event should bucket under in the given tz.

    Timed events: convert start dateTime to tz, return that local date.
    All-day events: return the raw `date` field parsed, with no UTC
    conversion. (A "2026-05-03" all-day event is May 3 in the calendar's
    own tz, not UTC midnight which would land it on May 2 in CSM tz.)
    """
    start = event.get("start", {})
    if "dateTime" in start:
        try:
            dt = datetime.fromisoformat(start["dateTime"])
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).date()
    if "date" in start:
        try:
            return date.fromisoformat(start["date"])
        except (TypeError, ValueError):
            return None
    return None


def _empty_range_result(start_date: date, end_date: date, tz_name: str, error: str | None = None) -> dict:
    """Return the empty-shape result for a calendar range query."""
    by_date: dict[str, dict] = {}
    cursor = start_date
    # Guard against inverted ranges so we still produce a usable shape.
    if cursor <= end_date:
        while cursor <= end_date:
            by_date[cursor.isoformat()] = {"customer_meetings": [], "internal_meetings": []}
            cursor += timedelta(days=1)
    out: dict = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": tz_name,
        "by_date": by_date,
        "total_customer_meetings": 0,
        "total_meetings": 0,
    }
    if error is not None:
        out["error"] = error[:200]
    return out


def _fetch_events_for_range(
    csm_id: str,
    start_date: date,
    end_date: date,
    tz: ZoneInfo,
) -> tuple[list[dict], str | None]:
    """Fetch raw events from Google Calendar for [start_date, end_date] inclusive.

    Day boundaries are computed in the given tz, then converted to UTC
    for the API timeMin / timeMax parameters. Returns (events, error).
    Caller decides how to shape the response.
    """
    day_start_local = datetime.combine(start_date, datetime.min.time(), tzinfo=tz)
    day_end_exclusive_local = datetime.combine(
        end_date + timedelta(days=1), datetime.min.time(), tzinfo=tz,
    )
    time_min = day_start_local.astimezone(timezone.utc).isoformat()
    time_max = day_end_exclusive_local.astimezone(timezone.utc).isoformat()

    creds = _load_credentials(csm_id)
    service = build("calendar", "v3", credentials=creds)

    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    except Exception as exc:
        logger.warning("[calendar] events.list error for %s: %s", csm_id, exc)
        return [], str(exc)

    return result.get("items", []) or [], None


def extract_calendar_range(
    csm_id: str,
    account_names: list[str],
    start_date: date,
    end_date: date,
) -> dict:
    """Connect to the CSM's Google Calendar and return events for [start_date, end_date].

    Both dates are interpreted in the CSM's stored timezone (defaulting to
    America/Chicago). The range is inclusive on both ends. Events are
    bucketed by their start date IN THE CSM TIMEZONE, so a 9pm Pacific
    meeting on May 3 buckets under May 3 even though it's already May 4 UTC.

    All-day events bucket on their `date` field directly (no UTC conversion).

    Args:
        csm_id: e.g. "sarah_c" or a Clerk user_id
        account_names: list of known account names for classification
        start_date: inclusive lower bound, in CSM tz
        end_date: inclusive upper bound, in CSM tz

    Returns:
        {
            "start_date": "YYYY-MM-DD",
            "end_date": "YYYY-MM-DD",
            "timezone": "America/Chicago",
            "by_date": {
                "YYYY-MM-DD": {"customer_meetings": [...], "internal_meetings": [...]},
                ...
            },
            "total_customer_meetings": int,
            "total_meetings": int,   # raw event count, includes personal events
        }

        On API error: same shape with empty by_date and an "error" key.
    """
    tz_name = _get_csm_tz_name(csm_id)
    tz = ZoneInfo(tz_name)

    if start_date > end_date:
        return _empty_range_result(
            start_date, end_date, tz_name,
            error=f"start_date {start_date.isoformat()} is after end_date {end_date.isoformat()}",
        )

    events, fetch_error = _fetch_events_for_range(csm_id, start_date, end_date, tz)
    if fetch_error is not None:
        return _empty_range_result(start_date, end_date, tz_name, error=fetch_error)

    # Pre-seed every date in the range so consumers can rely on the keys.
    by_date: dict[str, dict] = {}
    cursor = start_date
    while cursor <= end_date:
        by_date[cursor.isoformat()] = {"customer_meetings": [], "internal_meetings": []}
        cursor += timedelta(days=1)

    total_customer = 0

    for event in events:
        bucket = _event_bucket_date(event, tz)
        if bucket is None:
            continue
        bucket_key = bucket.isoformat()
        if bucket_key not in by_date:
            # Event spans outside the requested range. Possible when an
            # event ends after midnight in CSM tz on the last day of the
            # range, since Google returns events overlapping [timeMin, timeMax).
            continue

        classification = _classify_event(event, account_names)
        if classification == "personal":
            continue

        title = event.get("summary") or "(no title)"
        start_dt = _parse_event_time(event.get("start", {}))
        end_dt = _parse_event_time(event.get("end", {}))

        if start_dt is not None and start_dt.tzinfo is None:
            raise ValueError(
                f"extract_calendar_range: start_dt is naive for event "
                f"{event.get('id', '?')}. _parse_event_time must return tz-aware datetimes."
            )

        if start_dt is not None:
            local_start = start_dt.astimezone(tz)
            start_str = local_start.strftime("%H:%M")
            start_iso = local_start.isoformat()
        else:
            start_str = ""
            start_iso = None

        if end_dt is not None:
            local_end = end_dt.astimezone(tz)
            end_str = local_end.strftime("%H:%M")
        else:
            end_str = ""

        if classification == "customer":
            by_date[bucket_key]["customer_meetings"].append({
                "account": _match_account(event, account_names),
                "title": title,
                "start": start_str,
                "start_iso": start_iso,
                "end": end_str,
                "attendees": _extract_attendee_names(event),
                "has_prep_block": _has_prep_block(start_dt, events) if start_dt else False,
            })
            total_customer += 1
        elif classification == "internal":
            by_date[bucket_key]["internal_meetings"].append({
                "title": title,
                "start": start_str,
                "start_iso": start_iso,
                "end": end_str,
                "attendees": _extract_attendee_names(event),
            })

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": tz_name,
        "by_date": by_date,
        "total_customer_meetings": total_customer,
        "total_meetings": len(events),
    }


def extract_calendar_signals(csm_id: str, account_names: list[str]) -> dict:
    """Today's calendar signals in the CSM's local timezone.

    Thin shim over _fetch_events_for_range. Resolves "today" in the CSM's
    stored timezone, fetches events for that local day, classifies and
    shapes them in the legacy single-day format used by the digest pipeline,
    timeline endpoint, send_digest, and the chat tool slimming layer.

    The timeline endpoint relies on start_iso being a UTC "Z" string, so
    this shim emits start_iso in that legacy form rather than the tz-aware
    CSM-tz form used by extract_calendar_range.

    Args:
        csm_id: e.g. "sarah_c" or a Clerk user_id
        account_names: list of known account names for classification

    Returns:
        {
            "date": "YYYY-MM-DD",     # today in CSM tz
            "customer_meetings": [...],
            "internal_meetings": [...],
            "total_customer_meetings": int,
            "total_meetings": int,
            "available_gaps": ["HH:MM-HH:MM", ...],
        }
    """
    tz_name = _get_csm_tz_name(csm_id)
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()

    events, fetch_error = _fetch_events_for_range(csm_id, today, today, tz)
    if fetch_error is not None:
        return {
            "date": today.isoformat(),
            "customer_meetings": [],
            "internal_meetings": [],
            "total_customer_meetings": 0,
            "total_meetings": 0,
            "available_gaps": [],
        }

    customer_meetings: list[dict] = []
    internal_meetings: list[dict] = []

    for event in events:
        # Skip events that bucket on a different CSM-tz day. With the
        # CSM-tz-aligned timeMin/timeMax, this should be rare; keep the
        # guard so a multi-day all-day event doesn't double-count.
        bucket = _event_bucket_date(event, tz)
        if bucket is not None and bucket != today:
            continue

        classification = _classify_event(event, account_names)
        title = event.get("summary") or "(no title)"
        start_dt = _parse_event_time(event.get("start", {}))
        end_dt = _parse_event_time(event.get("end", {}))

        if start_dt is not None and start_dt.tzinfo is None:
            raise ValueError(
                f"extract_calendar_signals: start_dt is naive for event "
                f"{event.get('id', '?')}. _parse_event_time must return tz-aware datetimes."
            )

        # Display HH:MM in CSM tz, not UTC. start_iso is preserved in the
        # legacy UTC "Z" form for timeline-endpoint compatibility.
        if start_dt is not None:
            start_str = start_dt.astimezone(tz).strftime("%H:%M")
            start_iso = start_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            start_str = ""
            start_iso = None

        if end_dt is not None:
            end_str = end_dt.astimezone(tz).strftime("%H:%M")
        else:
            end_str = ""

        if classification == "customer":
            customer_meetings.append({
                "account": _match_account(event, account_names),
                "title": title,
                "start": start_str,
                "start_iso": start_iso,
                "end": end_str,
                "attendees": _extract_attendee_names(event),
                "has_prep_block": _has_prep_block(start_dt, events) if start_dt else False,
            })
        elif classification == "internal":
            internal_meetings.append({
                "title": title,
                "start": start_str,
                "start_iso": start_iso,
                "end": end_str,
                "attendees": _extract_attendee_names(event),
            })
        # "personal" events are excluded from output but included in gap calculation

    return {
        "date": today.isoformat(),
        "customer_meetings": customer_meetings,
        "internal_meetings": internal_meetings,
        "total_customer_meetings": len(customer_meetings),
        "total_meetings": len(events),
        "available_gaps": _compute_available_gaps(events, tz_name=tz_name),
    }
