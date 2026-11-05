"""
product/postcall/builder.py

Post-call draft builder for the post-call recap pipeline (C.1 Commit 2).

Takes a transcript text plus light event/account context and produces
two structured sections via a single Claude call:
  • recap_text         — 2-3 paragraphs in the CSM's first-person voice
  • timeline_entry_text — single paragraph formatted for HubSpot/Gainsight

Transcript content is never persisted server-side: the caller hands us
the string, we pass it to the LLM, we return the two generated sections,
and we drop the transcript. API errors propagate so the caller decides
retry vs. fallback; 529 (overloaded) gets the same 3-attempt retry as
product/digest/builder.py.

Pure function, no production caller in this commit — wired in Commit 5.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import anthropic


logger = logging.getLogger("cs-agent.postcall.builder")


# ── Config ───────────────────────────────────────────────────────────────────

from product.settings import ANTHROPIC_MODEL
MAX_TOKENS = 1024
TEMPERATURE = 0.0

RECAP_DELIMITER = "RECAP:"
TIMELINE_DELIMITER = "TIMELINE_ENTRY:"

_MAX_ERROR_SNIPPET = 200  # chars of LLM output surfaced on parse failures


# ── Exceptions ───────────────────────────────────────────────────────────────

class PostCallParseError(ValueError):
    """Raised when the LLM response can't be split into RECAP: and
    TIMELINE_ENTRY: sections. Carries a truncated copy of the output so
    log lines have something to debug against without dumping a full
    transcript-sized string.
    """

    def __init__(self, reason: str, *, llm_output: str):
        truncated = (
            llm_output[:_MAX_ERROR_SNIPPET] + "…"
            if len(llm_output) > _MAX_ERROR_SNIPPET
            else llm_output
        )
        super().__init__(f"{reason}; llm_output={truncated!r}")
        self.reason = reason
        self.llm_output = llm_output


# ── Client ───────────────────────────────────────────────────────────────────

def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
    return anthropic.Anthropic(api_key=api_key)


# ── Prompt assembly ──────────────────────────────────────────────────────────

def _build_system_prompt(csm_name: str) -> str:
    return f"""You are CS Agent, an AI assistant writing a post-call recap for {csm_name} immediately after their customer meeting. The transcript is fresh; your job is to extract what was said, what was agreed, and what {csm_name} owes next — nothing else.

Output exactly this structure, with a blank line between sections:

{RECAP_DELIMITER}
[2-3 short paragraphs in first person from {csm_name}'s perspective. What was discussed. What the customer team committed to. What {csm_name} committed to. What stayed unresolved. Do not recap meeting logistics ("we met for 30 minutes"); get straight to substance.]

{TIMELINE_DELIMITER}
[Single paragraph suitable for pasting into HubSpot as a note or Gainsight as a timeline activity. Third person, past tense, dense. Include attendees, topics, decisions, and next steps in compact form. No bullet lists — CRMs render them unreliably. Under 120 words.]

Rules:
- Do not invent details not in the transcript. If the transcript is ambiguous, say so rather than guess.
- Use standard customer success terminology: "timeline entry", "CTA", "health score".
- No meta-commentary, no "Here is your recap", no section headers other than the two delimiters above.
- First line of your output is exactly "{RECAP_DELIMITER}" — nothing before it."""


def _format_attendees(event: dict) -> str:
    attendees = event.get("attendees") or []
    names: list[str] = []
    for a in attendees:
        if a.get("self"):
            continue
        name = a.get("displayName") or a.get("email") or ""
        if name:
            names.append(name)
    return ", ".join(names) if names else "(not listed)"


def _format_account_context(ctx: dict | None) -> str:
    if not ctx:
        return ""
    lines = []
    for k, v in ctx.items():
        if v is None or v == "":
            continue
        lines.append(f"{k}: {v}")
    return "\n".join(lines)


def _build_user_message(
    *,
    transcript_text: str,
    event: dict,
    account_context: dict | None,
    previous_call_summary: str | None,
) -> str:
    sections = [
        "=== MEETING ===",
        f"Title: {event.get('summary', '(no title)')}",
        f"End time: {(event.get('end') or {}).get('dateTime', '(unknown)')}",
        f"Attendees: {_format_attendees(event)}",
    ]

    ctx_block = _format_account_context(account_context)
    if ctx_block:
        sections.append("\n=== ACCOUNT CONTEXT ===")
        sections.append(ctx_block)

    if previous_call_summary:
        sections.append("\n=== PREVIOUS CALL ===")
        sections.append(previous_call_summary.strip())

    sections.append("\n=== TRANSCRIPT ===")
    sections.append(transcript_text)

    return "\n".join(sections)


# ── Output parsing ───────────────────────────────────────────────────────────

def _parse_sections(text: str) -> tuple[str, str]:
    """Split the LLM output on RECAP: / TIMELINE_ENTRY: into two bodies.

    Forgiving of leading preamble (occasional LLM "Sure, here's the recap:"
    noise) and any whitespace variant between/around sections. Strict on
    missing delimiters and empty-after-strip bodies — those raise
    PostCallParseError with a truncated copy of the output.
    """
    if RECAP_DELIMITER not in text:
        raise PostCallParseError(
            f"missing {RECAP_DELIMITER} delimiter",
            llm_output=text,
        )
    if TIMELINE_DELIMITER not in text:
        raise PostCallParseError(
            f"missing {TIMELINE_DELIMITER} delimiter",
            llm_output=text,
        )
    _, after_recap = text.split(RECAP_DELIMITER, 1)
    recap_raw, timeline_raw = after_recap.split(TIMELINE_DELIMITER, 1)
    recap = recap_raw.strip()
    timeline = timeline_raw.strip()
    if not recap:
        raise PostCallParseError("RECAP section is empty", llm_output=text)
    if not timeline:
        raise PostCallParseError("TIMELINE_ENTRY section is empty", llm_output=text)
    return recap, timeline


# ── Main function ────────────────────────────────────────────────────────────

def build_postcall_draft(
    *,
    transcript_text: str,
    event: dict,
    csm_name: str,
    account_context: dict | None = None,
    previous_call_summary: str | None = None,
) -> dict:
    """Generate a post-call recap and timeline entry from a meeting transcript.

    Args:
        transcript_text: The meeting transcript as plain text. Caller fetches
            from Drive; the builder never reads Drive itself.
        event: Raw Google Calendar event dict (same shape as Commit 1's
            matcher). Used for title / end time / attendees in the context
            block.
        csm_name: CSM display name — anchors the first-person voice.
        account_context: Optional dict of account-grounding facts
            (e.g. {"name": "Meridian Health", "renewal_days": 42,
            "health_score": 78}). Keys are surfaced verbatim as
            "{key}: {value}" lines in the prompt.
        previous_call_summary: Optional text from the last call's recap,
            to help the LLM notice continuity.

    Returns:
        {
          "recap_text": str,            # first-person, 2-3 paragraphs
          "timeline_entry_text": str,   # third-person, single paragraph
          "model": str,                 # settings.ANTHROPIC_MODEL
          "input_tokens": int,
          "output_tokens": int,
        }

    Raises:
        ValueError: transcript_text is empty or whitespace-only (raised
            before any LLM call).
        PostCallParseError: LLM output couldn't be split into the two
            required sections.
        anthropic.APIError (and subclasses): API failures propagate.
            529 (overloaded) is retried up to 3 times with a 2-second
            sleep, matching product/digest/builder.py. Other API errors
            propagate immediately.
    """
    if not transcript_text or not transcript_text.strip():
        raise ValueError("transcript_text is empty")

    client = _get_client()
    system_prompt = _build_system_prompt(csm_name)
    user_message = _build_user_message(
        transcript_text=transcript_text,
        event=event,
        account_context=account_context,
        previous_call_summary=previous_call_summary,
    )

    response: Any = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            break
        except anthropic.APIStatusError as api_err:
            if api_err.status_code == 529 and attempt < 2:
                logger.warning(
                    "anthropic 529 (overloaded) on attempt %d; retrying", attempt + 1
                )
                time.sleep(2)
                continue
            raise
    if response is None:
        raise RuntimeError("Anthropic API unavailable after 3 retries (529)")

    output_text = response.content[0].text.strip()
    recap_text, timeline_entry_text = _parse_sections(output_text)

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
    output_tokens = getattr(usage, "output_tokens", 0) if usage else 0

    return {
        "recap_text": recap_text,
        "timeline_entry_text": timeline_entry_text,
        "model": ANTHROPIC_MODEL,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
