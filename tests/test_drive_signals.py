"""
tests/test_drive_signals.py

Unit tests for product/signals/drive.py — find_meet_transcripts and the
tab-aware extractor.

Discovery is now Calendar-attachment-based: the function lists recent
Calendar events, reads each event's `attachments` for Meet Doc file_ids,
then fetches Drive metadata + Docs body per unique file_id with
`includeTabsContent=True`. Tests mock all three Google API services
(calendar / drive / docs) so no live network calls happen.

These tests cover both the legacy single-tab body extraction path (the
[DEMO SEED] fixtures Sarah authored manually) and the multi-tab Meet
artifact path (Notes + Transcript tabs as Google produces today).

Standalone-runnable:
    python3 tests/test_drive_signals.py
"""

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.signals import drive, drive_cache
from googleapiclient.errors import HttpError


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Clear the snippet cache and rebind the module-level semaphore.

    asyncio.Semaphore binds to the first event loop it sees. Tests use
    asyncio.run(), which creates a new loop each time, so reset the
    semaphore to a fresh instance for each test.
    """
    drive_cache.clear()
    drive._DOCS_FETCH_SEMAPHORE = asyncio.Semaphore(5)
    yield
    drive_cache.clear()
    drive._DOCS_FETCH_SEMAPHORE = asyncio.Semaphore(5)


# ── Fixture builders ─────────────────────────────────────────────────────────


def _doc_attachment(file_id: str) -> dict:
    return {
        "fileId": file_id,
        "fileUrl": f"https://docs.google.com/document/d/{file_id}/edit?usp=meet_tnfm_calendar",
        "title": "Notes by Gemini",
        "mimeType": "application/vnd.google-apps.document",
    }


def _meet_event(idx: int, doc_file_ids: list[str]) -> dict:
    """Synthetic Calendar event with conferenceId + Meet doc attachments."""
    return {
        "id": f"evt_{idx}",
        "summary": f"Sync {idx}",
        "start": {"dateTime": f"2026-04-{20 + idx:02d}T14:00:00Z"},
        "end": {"dateTime": f"2026-04-{20 + idx:02d}T14:30:00Z"},
        "conferenceData": {"conferenceId": f"meet-conf-{idx}"},
        "attachments": [_doc_attachment(fid) for fid in doc_file_ids],
    }


def _make_events_with_files(n: int) -> tuple[list[dict], list[dict]]:
    """Build n events each carrying one unique Meet Doc attachment.

    Returns (events_list, file_metas). file_metas mirrors the shape that
    drive.files.get(...).execute() would return, indexed alongside events.
    """
    events = []
    metas = []
    for i in range(n):
        file_id = f"file_{i}"
        events.append(_meet_event(i, [file_id]))
        metas.append({
            "id": file_id,
            "name": f"Meeting transcript - 2026-04-{20 + i:02d}",
            "createdTime": f"2026-04-{20 + i:02d}T10:00:00Z",
            "modifiedTime": f"2026-04-{20 + i:02d}T11:00:00Z",
            "webViewLink": f"https://docs.google.com/document/d/{file_id}/edit",
        })
    return events, metas


def _build_doc_response(text: str) -> dict:
    """Legacy single-tab Doc shape — top-level body.content."""
    return {
        "body": {
            "content": [
                {"paragraph": {"elements": [{"textRun": {"content": text}}]}},
            ]
        }
    }


def _build_multi_tab_response(notes_text: str, transcript_text: str) -> dict:
    """Multi-tab Meet artifact shape returned when includeTabsContent=True.

    Mirrors what we observed empirically on Sarah's actual Meet artifact:
    tab 0 = Notes (Gemini summary), tab 1 = Transcript (verbatim turns).
    """
    return {
        "tabs": [
            {
                "tabProperties": {"title": "Notes", "tabId": "t.notes", "index": 0},
                "documentTab": {
                    "body": {
                        "content": [
                            {"paragraph": {"elements": [{"textRun": {"content": notes_text}}]}},
                        ]
                    }
                },
            },
            {
                "tabProperties": {"title": "Transcript", "tabId": "t.transcript", "index": 1},
                "documentTab": {
                    "body": {
                        "content": [
                            {"paragraph": {"elements": [{"textRun": {"content": transcript_text}}]}},
                        ]
                    }
                },
            },
        ]
    }


def _build_notes_only_multi_tab_response(notes_text: str) -> dict:
    """Multi-tab Doc with only a Notes tab (no Transcript tab) — the
    edge case that surfaces non-English Workspace localization.
    """
    return {
        "tabs": [
            {
                "tabProperties": {"title": "Notes", "tabId": "t.notes", "index": 0},
                "documentTab": {
                    "body": {
                        "content": [
                            {"paragraph": {"elements": [{"textRun": {"content": notes_text}}]}},
                        ]
                    }
                },
            },
        ]
    }


# ── Service fakes ────────────────────────────────────────────────────────────


class _FakeCalendarService:
    def __init__(self, events_or_exc):
        self._events_or_exc = events_or_exc

    def events(self):
        return self

    def list(self, **_kwargs):
        outer = self

        class _Req:
            def execute(self_inner):
                if isinstance(outer._events_or_exc, Exception):
                    raise outer._events_or_exc
                return {"items": outer._events_or_exc}

        return _Req()


class _FakeDriveService:
    """Drive v3 fake. Supports files().get(fileId=..., fields=...).execute()."""

    def __init__(self, meta_handler):
        self._meta_handler = meta_handler

    def files(self):
        return self

    def get(self, fileId: str, fields: str = ""):
        outer = self

        class _Req:
            def execute(self_inner):
                return outer._meta_handler(fileId)

        return _Req()


class _FakeDocsService:
    """Docs v1 fake. Supports documents().get(documentId=..., includeTabsContent=...)."""

    def __init__(self, doc_handler):
        self._doc_handler = doc_handler

    def documents(self):
        return self

    def get(self, documentId: str, includeTabsContent: bool = False):
        outer = self

        class _Req:
            def execute(self_inner):
                return outer._doc_handler(documentId, includeTabsContent)

        return _Req()


def _build_patch(events_or_exc, meta_handler, doc_handler):
    """Patch credential loader and googleapiclient.discovery.build inside drive.py."""
    fake_cal = _FakeCalendarService(events_or_exc)
    fake_drive = _FakeDriveService(meta_handler)
    fake_docs = _FakeDocsService(doc_handler)

    def _build_side_effect(api: str, version: str, credentials=None):
        if api == "calendar":
            return fake_cal
        if api == "drive":
            return fake_drive
        if api == "docs":
            return fake_docs
        raise AssertionError(f"unexpected api: {api}")

    return patch.multiple(
        "product.signals.drive",
        _load_credentials=MagicMock(return_value=MagicMock()),
        build=MagicMock(side_effect=_build_side_effect),
    )


def _meta_handler_from_metas(metas: list[dict]):
    by_id = {m["id"]: m for m in metas}

    def _handler(file_id: str) -> dict:
        if file_id not in by_id:
            raise AssertionError(f"unexpected drive.files.get for {file_id}")
        return by_id[file_id]

    return _handler


# ── tests: find_meet_transcripts (discovery + caching) ──────────────────────


def test_cold_cache_all_fetches_happen_and_are_cached():
    events, metas = _make_events_with_files(10)
    fetch_count = {"n": 0}

    def doc_handler(doc_id: str, include_tabs: bool) -> dict:
        assert include_tabs is True, "must request tabs content"
        fetch_count["n"] += 1
        return _build_doc_response(f"body for {doc_id}")

    with _build_patch(events, _meta_handler_from_metas(metas), doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    assert len(result) == 10
    assert fetch_count["n"] == 10

    for i in range(10):
        cached = drive_cache.get_snippet(f"file_{i}", f"2026-04-{20 + i:02d}T11:00:00Z")
        assert cached == f"body for file_{i}"


def test_warm_cache_no_docs_get_calls():
    events, metas = _make_events_with_files(10)

    for m in metas:
        drive_cache.set_snippet(m["id"], m["modifiedTime"], f"prewarmed snippet {m['id']}")

    fetch_count = {"n": 0}

    def doc_handler(doc_id: str, include_tabs: bool) -> dict:
        fetch_count["n"] += 1
        return _build_doc_response("UNEXPECTED")

    with _build_patch(events, _meta_handler_from_metas(metas), doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    assert fetch_count["n"] == 0
    assert len(result) == 10
    snippets = [t["content_snippet"] for t in result]
    assert all(s.startswith("prewarmed snippet ") for s in snippets)


def test_one_doc_timeout_returns_empty_snippet_others_succeed():
    events, metas = _make_events_with_files(10)

    def doc_handler(doc_id: str, include_tabs: bool) -> dict:
        if doc_id == "file_3":
            raise asyncio.TimeoutError("timed out")
        return _build_doc_response(f"body for {doc_id}")

    with _build_patch(events, _meta_handler_from_metas(metas), doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    by_id = {t["file_id"]: t for t in result}
    assert by_id["file_3"]["content_snippet"] == ""
    for i in range(10):
        if i == 3:
            continue
        assert by_id[f"file_{i}"]["content_snippet"] == f"body for file_{i}"

    assert drive_cache.get_snippet("file_3", "2026-04-23T11:00:00Z") is None
    assert drive_cache.get_snippet("file_0", "2026-04-20T11:00:00Z") == "body for file_0"


def test_one_doc_http_error_returns_empty_snippet_others_succeed():
    events, metas = _make_events_with_files(10)

    def doc_handler(doc_id: str, include_tabs: bool) -> dict:
        if doc_id == "file_5":
            resp = MagicMock(status=403, reason="forbidden")
            raise HttpError(resp, b"forbidden")
        return _build_doc_response(f"body for {doc_id}")

    with _build_patch(events, _meta_handler_from_metas(metas), doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    by_id = {t["file_id"]: t for t in result}
    assert by_id["file_5"]["content_snippet"] == ""
    successes = [t for fid, t in by_id.items() if fid != "file_5"]
    assert len(successes) == 9
    assert all(t["content_snippet"].startswith("body for ") for t in successes)


def test_returns_six_field_shape_and_correct_count():
    events, metas = _make_events_with_files(3)

    def doc_handler(doc_id: str, include_tabs: bool) -> dict:
        return _build_doc_response(f"body for {doc_id}")

    with _build_patch(events, _meta_handler_from_metas(metas), doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    assert len(result) == 3
    expected_keys = {"file_id", "title", "created_time", "modified_time", "web_link", "content_snippet"}
    for entry in result:
        assert set(entry.keys()) == expected_keys


def test_concurrency_capped_at_semaphore_limit():
    """Bound concurrent in-flight docs.get to the semaphore limit (5)."""
    events, metas = _make_events_with_files(20)

    in_flight = {"n": 0, "max": 0}
    in_flight_lock = threading.Lock()

    def doc_handler(doc_id: str, include_tabs: bool) -> dict:
        with in_flight_lock:
            in_flight["n"] += 1
            if in_flight["n"] > in_flight["max"]:
                in_flight["max"] = in_flight["n"]
        try:
            import time
            time.sleep(0.05)
            return _build_doc_response(f"body for {doc_id}")
        finally:
            with in_flight_lock:
                in_flight["n"] -= 1

    with _build_patch(events, _meta_handler_from_metas(metas), doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    assert len(result) == 20
    assert in_flight["max"] <= 5


def test_truncates_to_2000_chars():
    events, metas = _make_events_with_files(1)
    big = "x" * 5000

    def doc_handler(_doc_id: str, _include_tabs: bool) -> dict:
        return _build_doc_response(big)

    with _build_patch(events, _meta_handler_from_metas(metas), doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    assert len(result) == 1
    assert len(result[0]["content_snippet"]) == 2000


def test_calendar_list_failure_returns_empty_list():
    """If the calendar.events.list() call raises, function returns []."""

    def doc_handler(_doc_id: str, _include_tabs: bool) -> dict:
        return _build_doc_response("nope")

    def meta_handler(_file_id: str) -> dict:
        raise AssertionError("drive.files.get must not be called when calendar fails")

    with _build_patch(RuntimeError("calendar offline"), meta_handler, doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    assert result == []


def test_events_without_meet_conference_id_are_skipped():
    """Events without conferenceData.conferenceId yield no artifacts."""
    non_meet_event = {
        "id": "evt_no_meet",
        "summary": "Manual block",
        "attachments": [_doc_attachment("doc-stranded")],
        "conferenceData": {},
    }

    def meta_handler(_file_id: str) -> dict:
        raise AssertionError("drive.files.get must not be called for non-Meet events")

    def doc_handler(_doc_id: str, _include_tabs: bool) -> dict:
        raise AssertionError("docs.get must not be called for non-Meet events")

    with _build_patch([non_meet_event], meta_handler, doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    assert result == []


def test_duplicate_doc_ids_across_events_dedup_to_single_fetch():
    """Same Meet Doc attached to two events (rare but possible if the same
    file_id ever appears twice) → fetched once, returned once."""
    e1 = _meet_event(0, ["dup-doc"])
    e2 = _meet_event(1, ["dup-doc"])
    metas = [{
        "id": "dup-doc",
        "name": "Duplicate doc",
        "createdTime": "2026-04-20T10:00:00Z",
        "modifiedTime": "2026-04-20T11:00:00Z",
        "webViewLink": "https://docs.google.com/document/d/dup-doc/edit",
    }]

    fetch_count = {"n": 0}

    def doc_handler(_doc_id: str, _include_tabs: bool) -> dict:
        fetch_count["n"] += 1
        return _build_doc_response("body for dup-doc")

    with _build_patch([e1, e2], _meta_handler_from_metas(metas), doc_handler):
        result = asyncio.run(drive.find_meet_transcripts("user_x", days=14))

    assert len(result) == 1
    assert result[0]["file_id"] == "dup-doc"
    assert fetch_count["n"] == 1


# ── tests: tab-aware extractor (_extract_transcript_text) ────────────────────


def test_extract_transcript_text_multi_tab_returns_transcript_tab():
    """Multi-tab Doc with Notes + Transcript → returns Transcript tab text."""
    doc = _build_multi_tab_response(
        notes_text="GEMINI SUMMARY: project on track.",
        transcript_text="Sarah C: Hey Marcus. Marcus: Hey Sarah.",
    )
    text = drive._extract_transcript_text(doc, file_id="multi-tab-1")
    assert text == "Sarah C: Hey Marcus. Marcus: Hey Sarah."
    assert "GEMINI SUMMARY" not in text


def test_extract_transcript_text_multi_tab_no_transcript_tab_returns_empty_with_warn(caplog):
    """Multi-tab Doc with only a Notes tab → returns "" and logs WARN with file_id and tab titles."""
    doc = _build_notes_only_multi_tab_response("Notes-only content")
    with caplog.at_level("WARNING", logger="cs-agent.drive"):
        text = drive._extract_transcript_text(doc, file_id="notes-only-1")
    assert text == ""
    # WARN message must include file_id and tab titles for post-launch detection
    assert any(
        "transcript_tab_missing" in rec.message
        and "notes-only-1" in rec.message
        and "Notes" in rec.message
        for rec in caplog.records
    )


def test_extract_transcript_text_legacy_single_tab_walks_body():
    """Legacy [DEMO SEED] single-tab Doc (no `tabs` key) → walks top-level body."""
    doc = _build_doc_response("Legacy transcript text — Sarah and Marcus discussed renewal.")
    text = drive._extract_transcript_text(doc, file_id="legacy-1")
    assert text == "Legacy transcript text — Sarah and Marcus discussed renewal."


def test_extract_transcript_text_transcript_tab_match_is_case_insensitive():
    """tabProperties.title 'TRANSCRIPT' or 'transcript' both match."""
    doc_upper = {
        "tabs": [
            {
                "tabProperties": {"title": "TRANSCRIPT"},
                "documentTab": {
                    "body": {"content": [
                        {"paragraph": {"elements": [{"textRun": {"content": "upper-case match"}}]}},
                    ]}
                },
            },
        ]
    }
    assert drive._extract_transcript_text(doc_upper) == "upper-case match"


# ── tests: fetch_transcript_body integration with extractor ─────────────────


def test_fetch_transcript_body_calls_includeTabsContent_and_returns_transcript():
    """fetch_transcript_body must pass includeTabsContent=True and return the Transcript tab."""
    captured_kwargs = {}

    class _CapturingDocs:
        def documents(self):
            return self

        def get(self, **kwargs):
            captured_kwargs.update(kwargs)
            outer = self

            class _Req:
                def execute(self_inner):
                    return _build_multi_tab_response(
                        notes_text="Gemini notes summary text.",
                        transcript_text="Verbatim turns: hi / hello / bye.",
                    )

            return _Req()

    with patch.multiple(
        "product.signals.drive",
        _load_credentials=MagicMock(return_value=MagicMock()),
        build=MagicMock(return_value=_CapturingDocs()),
    ):
        result = drive.fetch_transcript_body("user_x", "abc")

    assert captured_kwargs.get("documentId") == "abc"
    assert captured_kwargs.get("includeTabsContent") is True
    assert result == "Verbatim turns: hi / hello / bye."


def test_fetch_transcript_body_legacy_single_tab_backward_compat():
    """Single-tab Doc returned by Docs API → fetch_transcript_body returns body text."""

    class _LegacyDocs:
        def documents(self):
            return self

        def get(self, **_kwargs):
            class _Req:
                def execute(self_inner):
                    return _build_doc_response("Single-tab demo transcript content.")

            return _Req()

    with patch.multiple(
        "product.signals.drive",
        _load_credentials=MagicMock(return_value=MagicMock()),
        build=MagicMock(return_value=_LegacyDocs()),
    ):
        result = drive.fetch_transcript_body("user_x", "legacy")

    assert result == "Single-tab demo transcript content."


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
