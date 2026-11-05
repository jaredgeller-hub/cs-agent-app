"""
product/postcall/artifacts.py

Calendar-event-attachment-based discovery of Google Meet artifacts
(Notes-by-Gemini Docs and recording .mp4 files).

Replaces the legacy filename-substring + creation-time-window heuristic
in product/postcall/matcher.py. Discovery is deterministic: every Meet
meeting carries an `attachments` array on its Calendar event listing the
exact file IDs of every artifact Google produced for that meeting.

Pure function — does not touch Drive or Docs. Caller decides what to
fetch. Does not raise.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("cs-agent.postcall.artifacts")


_DOC_MIME = "application/vnd.google-apps.document"
_RECORDING_MIME = "video/mp4"


def find_meet_artifacts_for_event(event: dict) -> dict:
    """Read Meet artifacts directly from a Calendar event's attachments.

    Args:
        event: Raw Google Calendar event dict (as returned by
            service.events().list().items[i]). Expected to optionally
            carry `attachments` and `conferenceData.conferenceId`.

    Returns:
        {
            "conference_id":        str,        # "" if not a Meet event
            "doc_file_ids":         list[str],  # mimeType == application/vnd.google-apps.document
            "recording_file_ids":   list[str],  # mimeType == video/mp4
            "other_attachments":    list[dict], # raw passthrough
        }

        Events without `conferenceData.conferenceId` (i.e. not a Meet
        meeting) return the empty shape — Calendar attachments on a
        manually scheduled non-Meet event would not be Meet artifacts.
    """
    empty = {
        "conference_id": "",
        "doc_file_ids": [],
        "recording_file_ids": [],
        "other_attachments": [],
    }

    if not isinstance(event, dict):
        return empty

    conference_id = (
        (event.get("conferenceData") or {}).get("conferenceId") or ""
    )
    if not conference_id:
        return empty

    attachments = event.get("attachments") or []
    if not isinstance(attachments, list):
        return empty

    doc_file_ids: list[str] = []
    recording_file_ids: list[str] = []
    other: list[dict] = []

    for att in attachments:
        if not isinstance(att, dict):
            continue
        file_id = att.get("fileId") or ""
        mime = att.get("mimeType") or ""
        if not file_id:
            continue
        if mime == _DOC_MIME:
            doc_file_ids.append(file_id)
        elif mime == _RECORDING_MIME:
            recording_file_ids.append(file_id)
        else:
            other.append(att)

    return {
        "conference_id": conference_id,
        "doc_file_ids": doc_file_ids,
        "recording_file_ids": recording_file_ids,
        "other_attachments": other,
    }
