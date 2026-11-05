"""
tests/test_postcall_artifacts.py

Unit tests for product/postcall/artifacts.find_meet_artifacts_for_event.

Pure-function tests — no Drive, no Calendar API, no credentials. Each
test feeds a synthetic Calendar event dict (mirroring the shape of
service.events().list().items[i]) and asserts the bucketed return.

Standalone-runnable:
    python3 tests/test_postcall_artifacts.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.postcall.artifacts import find_meet_artifacts_for_event


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _doc_attachment(file_id: str = "doc-abc", title: str = "Notes by Gemini") -> dict:
    return {
        "fileId": file_id,
        "fileUrl": f"https://docs.google.com/document/d/{file_id}/edit?usp=meet_tnfm_calendar",
        "title": title,
        "mimeType": "application/vnd.google-apps.document",
        "iconLink": "https://drive-thirdparty.googleusercontent.com/32/type/application/vnd.google-apps.document",
    }


def _recording_attachment(file_id: str = "rec-xyz", title: str = "Recording") -> dict:
    return {
        "fileId": file_id,
        "fileUrl": f"https://drive.google.com/file/d/{file_id}/view?usp=drive_web",
        "title": title,
        "mimeType": "video/mp4",
        "iconLink": "https://drive-thirdparty.googleusercontent.com/32/type/video/mp4",
    }


def _meet_event(attachments=None, conference_id="mqe-drmk-rvs") -> dict:
    return {
        "id": "evt-1",
        "summary": "Sarah / Marcus QBR",
        "start": {"dateTime": "2026-05-06T14:00:00Z"},
        "end": {"dateTime": "2026-05-06T15:00:00Z"},
        "conferenceData": {"conferenceId": conference_id} if conference_id else {},
        "attachments": attachments if attachments is not None else [],
    }


# ── Tests ────────────────────────────────────────────────────────────────────


def test_event_with_doc_and_mp4_attachments_buckets_correctly():
    """Event with both a Notes-by-Gemini Doc and a Recording .mp4 → both bucketed."""
    event = _meet_event(attachments=[
        _recording_attachment("rec-1"),
        _doc_attachment("doc-1"),
    ])
    result = find_meet_artifacts_for_event(event)

    assert result["conference_id"] == "mqe-drmk-rvs"
    assert result["doc_file_ids"] == ["doc-1"]
    assert result["recording_file_ids"] == ["rec-1"]
    assert result["other_attachments"] == []
    print("✓ Doc + .mp4 attachments bucket correctly")


def test_event_with_no_attachments_key_returns_empty_shape():
    """Event with no `attachments` field at all → empty shape, no raise."""
    event = _meet_event()
    # Strip the field entirely (vs. _meet_event default of [])
    del event["attachments"]
    result = find_meet_artifacts_for_event(event)

    assert result == {
        "conference_id": "mqe-drmk-rvs",
        "doc_file_ids": [],
        "recording_file_ids": [],
        "other_attachments": [],
    }
    print("✓ missing attachments key: empty shape")


def test_event_missing_conference_id_returns_empty_shape():
    """Event without `conferenceData.conferenceId` → empty shape (not a Meet meeting).

    Even if attachments are present, treat them as non-Meet artifacts —
    a manually scheduled event with a Drive Doc attached is not a Meet
    artifact, and the postcall flow should fall through to flow B/C.
    """
    event_no_cd = _meet_event(
        attachments=[_doc_attachment("doc-z")],
        conference_id=None,
    )
    result_no = find_meet_artifacts_for_event(event_no_cd)
    assert result_no["conference_id"] == ""
    assert result_no["doc_file_ids"] == []
    assert result_no["recording_file_ids"] == []
    assert result_no["other_attachments"] == []

    # Same for explicit empty string
    event_empty = _meet_event(
        attachments=[_doc_attachment("doc-z")],
        conference_id="",
    )
    result_empty = find_meet_artifacts_for_event(event_empty)
    assert result_empty["conference_id"] == ""
    assert result_empty["doc_file_ids"] == []

    print("✓ missing/empty conferenceId: empty shape")


def test_event_with_unknown_mime_type_passes_through_other():
    """Attachment of unknown MIME type → goes into other_attachments raw."""
    spreadsheet_att = {
        "fileId": "sheet-99",
        "title": "Action items tracker",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "iconLink": "https://drive-thirdparty.googleusercontent.com/32/type/sheet",
    }
    pdf_att = {
        "fileId": "pdf-77",
        "title": "Pre-read.pdf",
        "mimeType": "application/pdf",
    }
    event = _meet_event(attachments=[
        _doc_attachment("doc-keep"),
        spreadsheet_att,
        pdf_att,
    ])
    result = find_meet_artifacts_for_event(event)

    assert result["doc_file_ids"] == ["doc-keep"]
    assert result["recording_file_ids"] == []
    assert len(result["other_attachments"]) == 2
    other_ids = {a["fileId"] for a in result["other_attachments"]}
    assert other_ids == {"sheet-99", "pdf-77"}
    print("✓ unknown MIME types: other_attachments")


# ── Defensive cases (not in original 4-test brief, but cheap correctness) ────


def test_non_dict_input_returns_empty_shape():
    """find_meet_artifacts_for_event(None) and (str) and (list) → empty, no raise."""
    for bad in (None, "not a dict", [], 42):
        result = find_meet_artifacts_for_event(bad)
        assert result["doc_file_ids"] == []
        assert result["conference_id"] == ""


def test_attachment_missing_fileid_is_skipped():
    """Attachment without fileId is skipped (truncated/malformed) — no raise."""
    bad_att = {"title": "broken", "mimeType": "application/vnd.google-apps.document"}
    event = _meet_event(attachments=[bad_att, _doc_attachment("doc-good")])
    result = find_meet_artifacts_for_event(event)
    assert result["doc_file_ids"] == ["doc-good"]


def main():
    """Standalone runner."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v", "--timeout=30"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
