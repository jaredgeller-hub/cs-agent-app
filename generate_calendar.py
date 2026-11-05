"""
generate_calendar.py

Generates calendar.json for Sarah C., Senior CSM at Clarify.
Coverage: Feb 3, 2026 – Jun 19, 2026.

Run:
    python generate_calendar.py
"""

import json
from datetime import date, timedelta, datetime

TODAY      = date.today()
START_DATE = TODAY - timedelta(weeks=7)
END_DATE   = TODAY + timedelta(weeks=17)
NORTHGATE_CUTOFF = END_DATE - timedelta(weeks=3)

events = []
_counter = 0

def _make_id(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{_counter:04d}"

def _dt(d: date, h: int, m: int) -> datetime:
    return datetime(d.year, d.month, d.day, h, m)

def add(summary: str, start: datetime, end: datetime,
        attendees: list[str], kind: str, prefix: str) -> None:
    events.append({
        "id":        _make_id(prefix),
        "summary":   summary,
        "start":     start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end":       end.strftime("%Y-%m-%dT%H:%M:%S"),
        "attendees": attendees,
        "type":      kind,
    })

def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

def iso_week(d: date) -> int:
    return d.isocalendar()[1]

# ---------------------------------------------------------------------------
# Dates where QBRs replace regular customer check-ins (same account, same day)
# ---------------------------------------------------------------------------
QBR_SKIP = {
    date(2026, 3,  9),   # Beacon QBR — also a regular Beacon Monday (wk11, odd)
    date(2026, 4, 14),   # Sutton QBR  — also a regular Sutton Tuesday  (wk16, even)
}

# ---------------------------------------------------------------------------
# 1. STANDING WEEKLY — CS Team Standup  (Mon/Wed/Fri 9:00–9:30 AM)
# ---------------------------------------------------------------------------
STANDUP_DAYS = {0, 2, 4}   # Mon=0, Wed=2, Fri=4

for d in daterange(START_DATE, END_DATE):
    if d.weekday() in STANDUP_DAYS:
        add("CS Team Standup",
            _dt(d, 9, 0), _dt(d, 9, 30),
            [], "internal", "standup")

# ---------------------------------------------------------------------------
# 2. 1:1 with Manager — every other Tuesday, ODD ISO weeks
# ---------------------------------------------------------------------------
for d in daterange(START_DATE, END_DATE):
    if d.weekday() == 1 and iso_week(d) % 2 == 1:
        add("1:1 with Manager — Clarify",
            _dt(d, 14, 0), _dt(d, 14, 30),
            [], "internal", "1on1")

# ---------------------------------------------------------------------------
# 3. CS Pipeline Review — every other Thursday, EVEN ISO weeks
# ---------------------------------------------------------------------------
for d in daterange(START_DATE, END_DATE):
    if d.weekday() == 3 and iso_week(d) % 2 == 0:
        add("CS Pipeline Review",
            _dt(d, 12, 0), _dt(d, 13, 0),
            [], "internal", "pipeline")

# ---------------------------------------------------------------------------
# 4. CUSTOMER CHECK-INS
#    Each account alternates every 2 weeks; parity chosen so they land on the
#    first eligible occurrence within the range.
#
#    Meridian Health    — Wed, EVEN weeks  (first: Feb 4, wk6)
#    Northgate Logistics— Thu, EVEN weeks  (first: Feb 5, wk6) + absence signal
#    Beacon Financial   — Mon, ODD  weeks  (first: Feb 9, wk7)
#    Sutton Retail      — Tue, EVEN weeks  (first: Feb 3, wk6)
#    Pinnacle Group     — Fri, EVEN weeks  (first: Feb 6, wk6)
# ---------------------------------------------------------------------------

for d in daterange(START_DATE, END_DATE):
    wk = iso_week(d)
    wd = d.weekday()

    # Meridian Health — Wed, even weeks
    if wd == 2 and wk % 2 == 0 and d not in QBR_SKIP:
        add("Check-in — Meridian Health",
            _dt(d, 10, 0), _dt(d, 10, 45),
            ["James Okafor — Meridian Health"], "customer", "meridian")

    # Northgate Logistics — Thu, even weeks; absent last 3 weeks
    if wd == 3 and wk % 2 == 0 and d not in QBR_SKIP and d < NORTHGATE_CUTOFF:
        add("Check-in — Northgate Logistics",
            _dt(d, 10, 0), _dt(d, 10, 45),
            ["Dana Mills — Northgate Logistics"], "customer", "northgate")

    # Beacon Financial — Mon, odd weeks (most consistent)
    if wd == 0 and wk % 2 == 1 and d not in QBR_SKIP:
        add("Check-in — Beacon Financial",
            _dt(d, 13, 0), _dt(d, 14, 0),
            ["Priya Sharma — Beacon Financial"], "customer", "beacon")

    # Sutton Retail — Tue, even weeks
    if wd == 1 and wk % 2 == 0 and d not in QBR_SKIP:
        add("Check-in — Sutton Retail",
            _dt(d, 11, 0), _dt(d, 11, 45),
            ["Karen Wu — Sutton Retail"], "customer", "sutton")

    # Pinnacle Group — Fri, even weeks
    if wd == 4 and wk % 2 == 0 and d not in QBR_SKIP:
        add("Check-in — Pinnacle Group",
            _dt(d, 14, 0), _dt(d, 14, 45),
            ["Michael Torres — Pinnacle Group"], "customer", "pinnacle")

# ---------------------------------------------------------------------------
# 5. QBRs / EBRs  (each preceded by a 30-min Prep block)
# ---------------------------------------------------------------------------
QBRS = [
    # (date,           summary,                        sh, sm, eh, em,  attendees,                         prefix)
    (date(2026,  3,  9), "Beacon Financial QBR",       13,  0, 14, 30, ["Priya Sharma — Beacon Financial"],    "beacon-qbr"),
    (date(2026,  3, 25), "Meridian Health EBR",        10,  0, 11, 30, ["James Okafor — Meridian Health"],     "meridian-ebr"),
    (date(2026,  4, 14), "Sutton Retail QBR",          13,  0, 14, 30, ["Karen Wu — Sutton Retail"],           "sutton-qbr"),
    (date(2026,  4, 28), "Pinnacle Group QBR",         14,  0, 15, 30, ["Michael Torres — Pinnacle Group"],    "pinnacle-qbr"),
    (date(2026,  5, 12), "Northgate Logistics QBR",    10,  0, 11, 30, ["Dana Mills — Northgate Logistics"],   "northgate-qbr"),
]

for qbr_date, summary, sh, sm, eh, em, attendees, prefix in QBRS:
    # Extract account name for prep label
    account = summary.replace(" QBR", "").replace(" EBR", "")
    qbr_start = _dt(qbr_date, sh, sm)
    prep_start = qbr_start - timedelta(minutes=30)
    prep_end   = qbr_start

    add(f"Prep — {account}",
        prep_start, prep_end,
        [], "internal", f"prep-{prefix}")

    add(summary,
        _dt(qbr_date, sh, sm), _dt(qbr_date, eh, em),
        attendees, "customer", prefix)

# ---------------------------------------------------------------------------
# 6. PERSONAL BLOCKS
# ---------------------------------------------------------------------------

# Lunch / Focus Block — every Monday 12:00–1:00 PM
for d in daterange(START_DATE, END_DATE):
    if d.weekday() == 0:
        add("Lunch / Focus Block",
            _dt(d, 12, 0), _dt(d, 13, 0),
            [], "personal", "lunch")

# Gym — every Wednesday 5:30–6:30 PM
for d in daterange(START_DATE, END_DATE):
    if d.weekday() == 2:
        add("Gym",
            _dt(d, 17, 30), _dt(d, 18, 30),
            [], "personal", "gym")

# One-off personal appointments
add("Doctor Appointment",
    _dt(date(2026, 4,  3), 15, 0), _dt(date(2026, 4,  3), 16, 0),
    [], "personal", "doctor")

add("Dentist",
    _dt(date(2026, 5,  7),  9, 0), _dt(date(2026, 5,  7), 10, 0),
    [], "personal", "dentist")

# ---------------------------------------------------------------------------
# Sort by start time and write output
# ---------------------------------------------------------------------------
events.sort(key=lambda e: e["start"])

# -----------------------------------------------------------------------
# DEMO DAY EVENTS — fixed meetings stamped to today on every script run
# These match the demo script exactly and must not change.
# -----------------------------------------------------------------------
from datetime import date as _date
_today = _date.today().isoformat()
demo_day_events = [
    {
        "id": "demo-standup",
        "summary": "CS Team Standup",
        "start": f"{_today}T09:00:00",
        "end":   f"{_today}T09:30:00",
        "attendees": [],
        "type": "internal",
    },
    {
        "id": "demo-meridian",
        "summary": "Check-in — Meridian Health",
        "start": f"{_today}T09:30:00",
        "end":   f"{_today}T10:30:00",
        "attendees": ["Marcus Reyes — Meridian Health"],
        "type": "customer",
    },
    {
        "id": "demo-beacon",
        "summary": "Check-in — Beacon Financial",
        "start": f"{_today}T13:00:00",
        "end":   f"{_today}T14:00:00",
        "attendees": ["Priya Sharma — Beacon Financial"],
        "type": "customer",
    },
    {
        "id": "demo-sutton",
        "summary": "Check-in — Sutton Retail",
        "start": f"{_today}T14:30:00",
        "end":   f"{_today}T15:00:00",
        "attendees": ["Jordan Lee — Sutton Retail"],
        "type": "customer",
    },
]
# Remove any existing events on today's date, then add the fixed set
_today_str = _today
events = [e for e in events if not e.get("start", "").startswith(_today_str)]
events.extend(demo_day_events)

out_path = "calendar.json"
with open(out_path, "w") as f:
    json.dump(events, f, indent=2)

# Summary
customer = sum(1 for e in events if e["type"] == "customer")
internal = sum(1 for e in events if e["type"] == "internal")
personal = sum(1 for e in events if e["type"] == "personal")

print(f"Generated {len(events)} events → {out_path}")
print(f"  {customer} customer  |  {internal} internal  |  {personal} personal")
