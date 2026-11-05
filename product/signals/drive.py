"""
product/signals/drive.py

Google Drive signal extraction for the product pipeline.

Discovery is Calendar-attachment-based: every Google Meet meeting carries
an `attachments` array on its Calendar event listing the exact file IDs
of every artifact Google produced. We list recent calendar events, look
at each event's attachments, and fetch the Doc(s) we find. This replaces
the legacy filename-substring search ("name contains 'transcript'") which
silently missed Notes-by-Gemini Docs (Google's current default output).

Reading is tab-aware: as of 2026, Meet artifacts are multi-tab Docs with
tab 0 = "Notes" (Gemini summary) and tab 1 = "Transcript" (verbatim).
The Docs API returns only tab 0's `body` unless `includeTabsContent=True`
is passed; we set that flag and walk to the Transcript-titled tab so
downstream callers see the verbatim transcript, not the AI summary.

Falls back to the top-level body for legacy single-tab Docs (preserves
[DEMO SEED] fixture behavior).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from product.signals import drive_cache

logger = logging.getLogger("cs-agent.drive")

_DOCS_FETCH_SEMAPHORE = asyncio.Semaphore(5)
_DOCS_FETCH_TIMEOUT_S = 5.0

_TRANSCRIPT_TAB_TITLE = "transcript"


def _load_credentials(csm_id: str):
    from product.auth_utils import load_google_credentials
    return load_google_credentials(csm_id)


async def find_meet_transcripts(csm_id: str, days: int = 7) -> list[dict]:
    """Find Google Meet artifact Docs for events in the last N days.

    Discovery walks the CSM's primary calendar, filters to events with
    `conferenceData.conferenceId` (Meet meetings), and reads each event's
    `attachments` array via product.postcall.artifacts.find_meet_artifacts_for_event.

    Per-doc body fetches fan out concurrently bounded by a module-level
    asyncio.Semaphore(5) and consult the in-process LRU snippet cache
    keyed on (file_id, modified_time) before hitting the network. Docs
    fetched with `includeTabsContent=True`; snippet text is the verbatim
    Transcript tab when present, falling back to body for legacy
    single-tab Docs.

    Returns a list of transcript metadata dicts:
        {file_id, title, created_time, modified_time, web_link, content_snippet}
    """
    from product.postcall.artifacts import find_meet_artifacts_for_event

    creds = _load_credentials(csm_id)

    # ── Step 1: list recent calendar events ──────────────────────────
    cal_service = build("calendar", "v3", credentials=creds)
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days)).isoformat()
    time_max = now.isoformat()

    def _list_events():
        return cal_service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        ).execute()

    try:
        events_result = await asyncio.to_thread(_list_events)
    except Exception as e:
        logger.warning("Calendar list for Meet transcript discovery failed: %s", e)
        return []

    events = events_result.get("items", []) or []

    # ── Step 2: collect unique Meet Doc file_ids across events ──────
    seen: set[str] = set()
    ordered_ids: list[str] = []
    for ev in events:
        artifacts = find_meet_artifacts_for_event(ev)
        for fid in artifacts["doc_file_ids"]:
            if fid not in seen:
                seen.add(fid)
                ordered_ids.append(fid)

    if not ordered_ids:
        logger.info("Found 0 Meet transcripts for %s (last %d days)", csm_id, days)
        return []

    # ── Step 3: fetch metadata + tab-aware snippet per file ─────────
    drive_service = build("drive", "v3", credentials=creds)
    docs_service = build("docs", "v1", credentials=creds)

    async def fetch_one(file_id: str) -> dict | None:
        try:
            async with _DOCS_FETCH_SEMAPHORE:
                meta = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: drive_service.files().get(
                            fileId=file_id,
                            fields="id, name, createdTime, modifiedTime, webViewLink",
                        ).execute()
                    ),
                    timeout=_DOCS_FETCH_TIMEOUT_S,
                )
        except Exception as e:
            logger.debug(
                "Drive metadata fetch failed for %s (%s): %s",
                file_id, type(e).__name__, e,
            )
            return None

        modified_time = meta.get("modifiedTime", "") or ""

        cached = drive_cache.get_snippet(file_id, modified_time)
        if cached is not None:
            snippet = cached
        else:
            snippet = ""
            try:
                async with _DOCS_FETCH_SEMAPHORE:
                    doc = await asyncio.wait_for(
                        asyncio.to_thread(
                            lambda: docs_service.documents().get(
                                documentId=file_id,
                                includeTabsContent=True,
                            ).execute()
                        ),
                        timeout=_DOCS_FETCH_TIMEOUT_S,
                    )
                snippet = _extract_transcript_text(doc, file_id=file_id)[:2000]
                drive_cache.set_snippet(file_id, modified_time, snippet)
            except Exception as e:
                logger.debug(
                    "Could not read transcript %s (%s): %s",
                    file_id, type(e).__name__, e,
                )
                snippet = ""

        return {
            "file_id": file_id,
            "title": meta.get("name", ""),
            "created_time": meta.get("createdTime", ""),
            "modified_time": modified_time,
            "web_link": meta.get("webViewLink", ""),
            "content_snippet": snippet,
        }

    fetched = await asyncio.gather(
        *[fetch_one(fid) for fid in ordered_ids],
        return_exceptions=False,
    )
    transcripts = [t for t in fetched if t is not None]

    logger.info("Found %d Meet transcripts for %s (last %d days)", len(transcripts), csm_id, days)
    return list(transcripts)


def fetch_transcript_body(csm_id: str, file_id: str) -> str:
    """Return the verbatim Transcript-tab text from a Google Doc.

    For multi-tab Meet artifacts, walks tabs[*].documentTab.body.content
    looking for a tab whose tabProperties.title matches "Transcript"
    (case-insensitive exact match). Returns that tab's plaintext.

    Falls back to the top-level body for legacy single-tab Docs (this
    preserves [DEMO SEED] fixture behavior and any manually authored
    transcript Docs).

    Returns "" and logs a WARNING if a multi-tab Doc has no Transcript
    tab — this is the signal to detect non-English Workspace localization
    or tab-rename edge cases. Caller (postcall scheduler) treats empty as
    miss and falls through to flow B/C.

    Propagates Drive / Docs API errors. The caller decides retry vs.
    fallback. Scope required: documents.readonly
    """
    creds = _load_credentials(csm_id)
    docs_service = build("docs", "v1", credentials=creds)
    doc = docs_service.documents().get(
        documentId=file_id,
        includeTabsContent=True,
    ).execute()
    return _extract_transcript_text(doc, file_id=file_id)


def _extract_transcript_text(doc: dict, file_id: str = "") -> str:
    """Tab-aware text extractor for Google Docs API responses.

    Order of preference:
      1. If `tabs` is present and a tab has tabProperties.title matching
         "Transcript" (case-insensitive exact), walk that tab's
         documentTab.body.content. Return its text.
      2. If `tabs` is present but no Transcript tab exists, return ""
         and log a structured WARNING with the file_id and the list of
         tab titles found. This is the post-launch detection signal for
         non-English Workspace tenants (filed as P3 Tech Debt).
      3. If `tabs` is absent (legacy single-tab Doc), walk the top-level
         body.content. This is the [DEMO SEED] backward-compat path.
    """
    tabs = doc.get("tabs")
    if isinstance(tabs, list) and tabs:
        tab_titles: list[str] = []
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            props = tab.get("tabProperties") or {}
            title = props.get("title") or ""
            tab_titles.append(title)
            if title.strip().lower() == _TRANSCRIPT_TAB_TITLE:
                doc_tab = tab.get("documentTab") or {}
                body = doc_tab.get("body") or {}
                return _walk_body(body)
        logger.warning(
            "transcript_tab_missing file_id=%s tab_titles=%s",
            file_id, tab_titles,
        )
        return ""

    body = doc.get("body") or {}
    return _walk_body(body)


def _walk_body(body: dict) -> str:
    """Walk a Google Docs body.content array and concatenate textRun text.

    Used by both the per-tab extractor and the legacy single-tab fallback
    path. Returns plain text with original whitespace preserved.
    """
    text_parts: list[str] = []
    for element in body.get("content", []) or []:
        paragraph = element.get("paragraph") or {}
        for elem in paragraph.get("elements") or []:
            run = elem.get("textRun") or {}
            content = run.get("content")
            if content:
                text_parts.append(content)
    return "".join(text_parts)
