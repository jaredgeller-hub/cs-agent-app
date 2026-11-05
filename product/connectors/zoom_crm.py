"""
product/connectors/zoom_crm.py

Zoom transcript retrieval via the Zoom Remote MCP Server
(https://mcp-us.zoom.us). Authenticates with the per-user OAuth token
managed in zoom_mcp.py.

Mirrors hubspot_crm.py's MCP client posture:
  - module-level circuit breaker (3 consecutive 429s -> 300s pause)
  - 0.2s inter-call delay after each successful response
  - 401 -> token refresh + retry once (under token_lock_async)
  - 429 -> back-off + retry up to 2x

Every public method is fail-closed: it returns [] / None on any error and
never raises, so a Zoom outage degrades the digest/recap rather than
breaking it.

AI Companion note: meeting assets (transcript/summary) only exist when the
host enabled Zoom AI Companion or cloud-recording transcription. When
get_meeting_assets comes back empty repeatedly we log a single
"zoom_ai_companion_unavailable" warning so the CSM can be nudged to check
their Zoom settings. This is a warning only for Stage 1, never an error.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger("cs-agent.zoom_crm")

# Zoom's Remote MCP server endpoint. The old bare host (https://mcp-us.zoom.us)
# now 302-redirects to https://www.zoom.com and breaks every tool call; the
# current streamable-HTTP endpoint per https://developers.zoom.us/docs/mcp/zoom/
# is the one below. Overridable via ZOOM_MCP_URL so a future move needs no code change.
_MCP_URL = os.environ.get("ZOOM_MCP_URL", "https://mcp.zoom.us/mcp/zoom/streamable")


# -- Circuit breaker -----------------------------------------------------------
_circuit_breaker_until: float = 0.0
_consecutive_429s: int = 0
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN = 300  # 5 minutes


def _check_circuit_breaker() -> bool:
    """Return True if MCP calls should be blocked (circuit is open)."""
    return time.time() < _circuit_breaker_until


def _record_429() -> None:
    global _consecutive_429s, _circuit_breaker_until
    _consecutive_429s += 1
    if _consecutive_429s >= _CIRCUIT_BREAKER_THRESHOLD:
        _circuit_breaker_until = time.time() + _CIRCUIT_BREAKER_COOLDOWN
        logger.warning(
            "[zoom_crm] Circuit breaker OPEN - pausing all MCP calls for %ds after %d consecutive 429s",
            _CIRCUIT_BREAKER_COOLDOWN, _consecutive_429s,
        )
        _consecutive_429s = 0


def _record_success() -> None:
    global _consecutive_429s
    _consecutive_429s = 0


# -- AI Companion degraded tracking --------------------------------------------
_consecutive_empty_assets: int = 0
_EMPTY_ASSETS_WARN_THRESHOLD = 3


def _record_assets_result(has_assets: bool) -> None:
    """Track consecutive empty get_meeting_assets responses.

    After _EMPTY_ASSETS_WARN_THRESHOLD consecutive empties, emit a single
    structured warning so the CSM can be nudged to check their Zoom AI
    Companion / cloud-recording transcription settings.
    """
    global _consecutive_empty_assets
    if has_assets:
        _consecutive_empty_assets = 0
        return
    _consecutive_empty_assets += 1
    if _consecutive_empty_assets == _EMPTY_ASSETS_WARN_THRESHOLD:
        logger.warning(json.dumps({
            "event": "zoom_ai_companion_unavailable",
            "detail": (
                f"{_consecutive_empty_assets} consecutive Zoom recordings returned no "
                "transcript assets - AI Companion / cloud transcription may be disabled"
            ),
        }))


# -- Async/sync bridge ---------------------------------------------------------

def _run(coro):
    """Run an async coroutine safely from synchronous code.

    Dedicated thread with its own event loop so this works even when called
    from within a running async context (FastAPI/uvicorn, arq).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


# -- MCP client ----------------------------------------------------------------

def _mcp_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _parse_content(content: list) -> object | None:
    """Extract a JSON-decoded payload from MCP content blocks.

    MCP tool results are a list of content blocks; the Zoom tools return a
    text block carrying JSON. Returns the decoded object, or None if no
    parseable text is present.
    """
    for block in content or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if not text:
            continue
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            # A plain-text block (e.g. a transcript body) is returned as-is.
            return text
    return None


async def _mcp_call_tool(csm_id: str, tool_name: str, arguments: dict) -> object | None:
    """Call a Zoom MCP tool. Returns the decoded payload, or None on failure.

    Handles 401 (token refresh + retry once, under the per-(csm_id,"zoom")
    lock) and 429 (back-off + retry up to 2x). A 0.2s inter-call delay is
    added after each successful response. Honors the circuit breaker.
    """
    from product.connectors.zoom_mcp import get_zoom_mcp_client, refresh_zoom_token

    client = get_zoom_mcp_client(csm_id)
    if not client:
        logger.warning("[zoom_crm] No Zoom token for %s, skipping %s", csm_id, tool_name)
        return None
    access_token = client["token"]

    if _check_circuit_breaker():
        logger.warning("[zoom_crm] Circuit breaker OPEN, skipping %s", tool_name)
        return None

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                headers=_mcp_headers(access_token),
                timeout=30.0,
            ) as http_client:
                async with streamable_http_client(_MCP_URL, http_client=http_client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments=arguments)
                        if result.isError:
                            logger.warning("[zoom_crm] tool %s returned isError=True: %s", tool_name, result.content)
                            return None
                        await asyncio.sleep(0.2)
                        _record_success()
                        return _parse_content(result.content)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401 and attempt == 0:
                logger.debug("[zoom_crm] 401 on attempt 1, refreshing token and retrying")
                stale = access_token
                from product.token_lock import token_lock_async
                async with token_lock_async(csm_id, "zoom"):
                    try:
                        refreshed = refresh_zoom_token(csm_id)
                    except Exception as refresh_exc:
                        logger.warning("[zoom_crm] Token refresh on 401 failed: %s", refresh_exc)
                        return None
                    if refreshed and refreshed.get("access_token"):
                        access_token = refreshed["access_token"]
                    elif refreshed is None:
                        return None
                if access_token == stale:
                    # Nothing actually changed; don't spin.
                    return None
                continue
            if status == 429 and attempt < 2:
                _record_429()
                wait = 1.0 * (attempt + 1)  # 1s, then 2s
                logger.warning("[zoom_crm] 429 on %s (attempt %d), retrying in %.1fs", tool_name, attempt + 1, wait)
                await asyncio.sleep(wait)
                continue
            logger.warning("[zoom_crm] HTTP error calling %s: %s", tool_name, exc)
            return None
        except Exception as exc:
            logger.warning("[zoom_crm] Error calling MCP tool %s: %s", tool_name, exc)
            return None
    return None


# -- Meeting-id encoding -------------------------------------------------------

def _needs_double_encode(meeting_id: str) -> bool:
    """Zoom meeting UUIDs containing /, +, or = often must be double-URL-
    encoded when used as the meetingId path param. Signal whether to retry."""
    return bool(meeting_id) and any(c in meeting_id for c in ("/", "+", "="))


def _double_encode(meeting_id: str) -> str:
    """Double URL-encode a meeting UUID for use as a path param."""
    return urllib.parse.quote(urllib.parse.quote(meeting_id, safe=""), safe="")


# -- Public methods (sync, fail-closed) ----------------------------------------

def get_recordings(csm_id: str, from_date: str, to_date: str) -> list[dict]:
    """List cloud recordings in [from_date, to_date] (YYYY-MM-DD).

    Returns a list of {recording_id, topic, start_time, duration, host_id}.
    Empty list on any error.
    """
    try:
        payload = _run(_mcp_call_tool(csm_id, "recordings_list", {
            "from": from_date,
            "to": to_date,
        }))
    except Exception as exc:
        logger.warning("[zoom_crm] get_recordings failed for %s: %s", csm_id, exc)
        return []

    meetings = []
    if isinstance(payload, dict):
        meetings = payload.get("meetings") or payload.get("recordings") or []
    elif isinstance(payload, list):
        meetings = payload

    out: list[dict] = []
    for m in meetings or []:
        if not isinstance(m, dict):
            continue
        recording_id = m.get("uuid") or m.get("id") or m.get("recording_id")
        if not recording_id:
            continue
        out.append({
            "recording_id": str(recording_id),
            "topic": m.get("topic", ""),
            "start_time": m.get("start_time", ""),
            "duration": m.get("duration", 0),
            "host_id": m.get("host_id", ""),
        })
    return out


def get_recording_details(
    csm_id: str,
    recording_id: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict | None:
    """Resolve a recording's topic, start_time, and transcript download_url.

    Looks the recording up in recordings_list (the same call get_recordings
    uses) and reads its recording_files array for the transcript file's
    direct download_url. This replaces get_meeting_assets, whose per-meeting
    meetingId path param consistently 404s ("Meeting does not exist") for
    cloud-recording UUIDs.

    Defaults the window to the last 30 days so a recent or reprocessed
    recording is in range. Matches recording_id by plain string equality
    against the entry's uuid / id / recording_id (the form proven to match
    in this path).

    Returns {"topic", "start_time", "download_url"} on a match (download_url
    is None when the matched entry has no transcript file), or None when no
    entry matches recording_id. Fail-closed: returns None on any error.
    """
    try:
        if not from_date or not to_date:
            now = datetime.now(timezone.utc)
            from_date = (now - timedelta(days=30)).date().isoformat()
            to_date = now.date().isoformat()

        payload = _run(_mcp_call_tool(csm_id, "recordings_list", {
            "from": from_date,
            "to": to_date,
        }))
    except Exception as exc:
        logger.warning("[zoom_crm] get_recording_details failed for %s: %s", csm_id, exc)
        return None

    meetings = []
    if isinstance(payload, dict):
        meetings = payload.get("meetings") or payload.get("recordings") or []
    elif isinstance(payload, list):
        meetings = payload

    for m in meetings or []:
        if not isinstance(m, dict):
            continue
        candidates = {
            m.get("uuid"),
            str(m.get("id")) if m.get("id") is not None else None,
            m.get("recording_id"),
        }
        if recording_id not in candidates:
            continue

        download_url = None
        for f in m.get("recording_files") or []:
            if not isinstance(f, dict):
                continue
            if f.get("file_type") == "TRANSCRIPT" or f.get("recording_type") == "audio_transcript":
                download_url = f.get("download_url")
                break

        return {
            "topic": m.get("topic", "") or "",
            "start_time": m.get("start_time", "") or "",
            "download_url": download_url,
        }

    return None


def _vtt_to_plaintext(text: str) -> str:
    """Strip WEBVTT scaffolding to clean, speaker-attributed plaintext.

    Zoom hands transcripts back as raw WEBVTT: a "WEBVTT" header, bare-integer
    cue sequence lines, "start --> end" timing lines, then one spoken-text line
    per cue carrying an inline "Speaker Name: text" prefix. Handed verbatim to
    the recap LLM this degrades quality versus the Meet path's clean prose.
    This removes the VTT scaffolding and collapses consecutive same-speaker
    cues into a single labeled block.

    Safe no-op: if the text does not look like VTT (no "WEBVTT" header AND no
    "-->" timing line) it is returned unchanged, so an already-clean transcript
    passes through untouched.

    Defensive: any line whose shape is unexpected (no detectable speaker prefix,
    etc.) is kept verbatim rather than dropped, so spoken content is never lost.
    """
    if not text:
        return text

    lines = text.splitlines()
    looks_like_vtt = "WEBVTT" in text or any("-->" in ln for ln in lines)
    if not looks_like_vtt:
        return text

    def _speaker_of(line: str) -> str | None:
        # The speaker label is the substring before the first ": ", but only
        # when it looks like a label: short and free of sentence punctuation.
        idx = line.find(": ")
        if idx <= 0:
            return None
        label = line[:idx].strip()
        if not label or len(label) >= 50:
            return None
        if any(p in label for p in (".", "!", "?")):
            return None
        return label

    blocks: list[str] = []
    last_speaker: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("WEBVTT"):  # header line, optionally "WEBVTT - title"
            continue
        if "-->" in line:  # cue timing line
            continue
        if line.isdigit():  # bare-integer cue sequence line
            continue

        speaker = _speaker_of(line)
        if speaker is not None and speaker == last_speaker:
            # Same speaker as the previous kept line: append the spoken text
            # (everything after "Speaker: ") to the existing block, no repeated
            # prefix.
            spoken = line[line.find(": ") + 2:].strip()
            if spoken:
                blocks[-1] = blocks[-1].rstrip() + " " + spoken
        else:
            # New speaker, or a line with no detectable speaker prefix: keep it
            # as its own line. last_speaker becomes None for prefix-less lines so
            # they never collapse into one another.
            blocks.append(line)
            last_speaker = speaker

    return "\n".join(blocks).strip()


def fetch_transcript_from_url(csm_id: str, download_url: str) -> str | None:
    """Fetch a transcript body from a Zoom recording_files download_url.

    Acquires the access token the same way _mcp_call_tool does
    (get_zoom_mcp_client). Follows redirects (Zoom download URLs commonly 302
    to a signed URL). On 401 refreshes the token once under the per-(csm_id,
    "zoom") lock and retries. Validates the body is a transcript (WEBVTT
    header or a "-->" cue) before returning. Fail-closed: returns None on any
    error / non-200 / non-transcript body.
    """
    from product.connectors.zoom_mcp import get_zoom_mcp_client, refresh_zoom_token

    try:
        client = get_zoom_mcp_client(csm_id)
        if not client:
            logger.warning("[zoom_crm] No Zoom token for %s, skipping transcript fetch", csm_id)
            return None
        access_token = client["token"]

        for attempt in range(2):
            resp = httpx.get(
                download_url,
                headers={"Authorization": f"Bearer {access_token}"},
                follow_redirects=True,
                timeout=30.0,
            )
            if resp.status_code == 401 and attempt == 0:
                logger.debug("[zoom_crm] 401 fetching transcript, refreshing token and retrying")
                stale = access_token
                from product.token_lock import token_lock
                with token_lock(csm_id, "zoom"):
                    try:
                        refreshed = refresh_zoom_token(csm_id)
                    except Exception as refresh_exc:
                        logger.warning("[zoom_crm] Token refresh on 401 failed: %s", refresh_exc)
                        return None
                    if refreshed and refreshed.get("access_token"):
                        access_token = refreshed["access_token"]
                    elif refreshed is None:
                        return None
                if access_token == stale:
                    return None
                continue

            if resp.status_code != 200:
                logger.warning(
                    "[zoom_crm] transcript fetch returned %d for %s", resp.status_code, csm_id,
                )
                return None

            text = resp.text
            if text and (text.startswith("WEBVTT") or "-->" in text):
                return _vtt_to_plaintext(text)
            logger.warning(
                "[zoom_crm] transcript fetch body is not a transcript (HTML/empty/login) for %s",
                csm_id,
            )
            return None
    except Exception as exc:
        logger.warning("[zoom_crm] fetch_transcript_from_url failed for %s: %s", csm_id, exc)
        return None

    return None


def get_meeting_assets(csm_id: str, meeting_id: str) -> dict | None:
    """Fetch transcript + summary + action items for a meeting.

    Returns {transcript_text, summary, action_items} or None when no assets
    exist (AI Companion / transcription disabled, or an error). Tracks
    consecutive empties for the degraded-status warning.
    """
    # Zoom MCP expects the camelCase "meetingId" path param. UUIDs with
    # /, +, or = may need double-URL-encoding; try raw first, then once
    # double-encoded if the raw form yielded no transcript.
    candidates = [(meeting_id, "raw")]
    if _needs_double_encode(meeting_id):
        candidates.append((_double_encode(meeting_id), "double_encoded"))

    for mid, form in candidates:
        try:
            payload = _run(_mcp_call_tool(csm_id, "get_meeting_assets", {
                "meetingId": mid,
            }))
        except Exception as exc:
            logger.warning("[zoom_crm] get_meeting_assets failed for %s: %s", csm_id, exc)
            payload = None

        if isinstance(payload, dict):
            transcript_text = (
                payload.get("transcript_text")
                or payload.get("transcript")
                or ""
            )
            if transcript_text and str(transcript_text).strip():
                logger.info(json.dumps({
                    "event": "zoom_meeting_id_form",
                    "form": form,
                    "tool": "get_meeting_assets",
                }))
                _record_assets_result(True)
                return {
                    "transcript_text": transcript_text,
                    "summary": payload.get("summary", ""),
                    "action_items": payload.get("action_items", []),
                }

    _record_assets_result(False)
    return None


def get_recording_resource(csm_id: str, meeting_id: str) -> str | None:
    """Fetch the raw transcript text for a meeting via get_recording_resource.

    Returns the transcript string, or None on error / absence. Used as a
    fallback when get_meeting_assets has no transcript.
    """
    # Same camelCase "meetingId" + double-encode retry posture as
    # get_meeting_assets.
    candidates = [(meeting_id, "raw")]
    if _needs_double_encode(meeting_id):
        candidates.append((_double_encode(meeting_id), "double_encoded"))

    for mid, form in candidates:
        try:
            payload = _run(_mcp_call_tool(csm_id, "get_recording_resource", {
                "meetingId": mid,
            }))
        except Exception as exc:
            logger.warning("[zoom_crm] get_recording_resource failed for %s: %s", csm_id, exc)
            payload = None

        text = None
        if isinstance(payload, str):
            text = payload or None
        elif isinstance(payload, dict):
            text = payload.get("transcript_text") or payload.get("transcript") or payload.get("content") or None
        if text:
            logger.info(json.dumps({
                "event": "zoom_meeting_id_form",
                "form": form,
                "tool": "get_recording_resource",
            }))
            return text

    return None


def search_meetings(csm_id: str, query: str, from_date: str, to_date: str) -> list[dict]:
    """Search meetings by query in [from_date, to_date]. Returns meeting dicts."""
    try:
        payload = _run(_mcp_call_tool(csm_id, "search_meetings", {
            "query": query,
            "from": from_date,
            "to": to_date,
        }))
    except Exception as exc:
        logger.warning("[zoom_crm] search_meetings failed for %s: %s", csm_id, exc)
        return []

    if isinstance(payload, dict):
        meetings = payload.get("meetings") or payload.get("results") or []
    elif isinstance(payload, list):
        meetings = payload
    else:
        meetings = []
    return [m for m in (meetings or []) if isinstance(m, dict)]
