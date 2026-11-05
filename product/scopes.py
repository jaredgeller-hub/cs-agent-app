"""Named constants for Google OAuth scope URLs.

Centralizes the Google scope strings used across the codebase so callers
can do `from product.scopes import CALENDAR_READONLY` instead of typing
the full URL. Mirrors the scopes documented in product/auth_google.py
(SCOPES_READONLY, SCOPES_WRITE, SCOPES_SEED).

Track L Step 4 lands this module as a pure addition (rollback anchor).
Sprint 6 follow-up commits will migrate existing literal-string usage
across main.py, auth_probe.py, and connectors to import from here.

Track L scoping doc: https://www.notion.so/3591d2f9aff081a087d5d0410fe862f2
"""

from __future__ import annotations

# Gmail
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"

# Calendar
CALENDAR_READONLY = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"

# Drive
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
DRIVE_MEET_READONLY = "https://www.googleapis.com/auth/drive.meet.readonly"

# Docs
DOCUMENTS_READONLY = "https://www.googleapis.com/auth/documents.readonly"

# Identity
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
OPENID = "openid"
