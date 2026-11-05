"""
tests/test_chat_tooluse_loop.py

Loop-mechanics tests for the new /chat tool-use handler. Mocks the
Anthropic client so messages.create returns canned tool_use + text
responses, mocks dispatch_tool to return canned tool outputs, and
mocks _resolve_auth + the Customer query so the subscription gate
allows a non-sarah_c csm_id through.

These tests verify the iteration loop, tool dispatch, partial failure,
max-iterations cap, and DRAFT_CARD parsing on the final text turn.
They do NOT verify Claude's actual decision-making — canned responses
bypass reasoning.

Standalone-runnable:
    python3 tests/test_chat_tooluse_loop.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


# ── Mock builders ────────────────────────────────────────────────────────────

def _text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_use_block(tool_name: str, input_dict: dict, tool_use_id: str) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = input_dict
    block.id = tool_use_id
    return block


def _make_response(content_blocks: list, stop_reason: str = "end_turn") -> MagicMock:
    """Mock anthropic response with given content list and stop_reason."""
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


def _make_anthropic_with_responses(responses: list) -> MagicMock:
    """Mock client whose .messages.create yields each response in order."""
    fake_client = MagicMock(name="anthropic_client")
    fake_client.messages.create.side_effect = list(responses)
    return fake_client


def _trialing_session_factory() -> MagicMock:
    """SessionLocal factory whose Customer row has subscription_status=trialing."""
    fake_row = MagicMock()
    fake_row.subscription_status = "trialing"
    fake_session = MagicMock()
    fake_session.query.return_value.filter_by.return_value.first.return_value = fake_row
    return MagicMock(return_value=fake_session)


def _fake_session() -> dict:
    return {"csm_id": "user_test123", "name": "Test User", "email": "test@example.com"}


def _post_chat(message: str = "what's on my plate?") -> dict:
    """Hit /chat as a non-sarah_c authenticated user with tooluse enabled."""
    response = client.post("/chat", json={"message": message})
    assert response.status_code == 200, f"unexpected status: {response.status_code} body={response.text}"
    return response.json()


# ── Tests ────────────────────────────────────────────────────────────────────


def test_single_tool_call_then_end_turn():
    """One tool_use turn followed by an end_turn text turn."""
    responses = [
        _make_response(
            [_tool_use_block("get_calendar_events", {}, "tu_1")],
            stop_reason="tool_use",
        ),
        _make_response(
            [_text_block("You have 2 customer meetings today: Acme at 10am and Beacon at 2pm.")],
            stop_reason="end_turn",
        ),
    ]
    fake_client = _make_anthropic_with_responses(responses)
    fake_dispatch = AsyncMock(return_value={
        "date": "2026-05-02",
        "customer_meetings": [
            {"account": "Acme", "title": "Acme QBR", "start": "10:00"},
            {"account": "Beacon", "title": "Beacon sync", "start": "14:00"},
        ],
        "internal_meetings": [],
        "total_customer_meetings": 2,
        "total_meetings": 2,
    })

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        data = _post_chat("what meetings do I have today?")

    assert fake_dispatch.await_count == 1
    assert fake_dispatch.await_args.args[0] == "get_calendar_events"
    assert "Acme" in data["reply"]


def test_two_tool_calls_in_one_assistant_turn():
    """A single assistant turn requesting two tools at once."""
    responses = [
        _make_response(
            [
                _tool_use_block("list_my_accounts", {}, "tu_1"),
                _tool_use_block("get_calendar_events", {}, "tu_2"),
            ],
            stop_reason="tool_use",
        ),
        _make_response(
            [_text_block("You have 3 accounts and 2 meetings today.")],
            stop_reason="end_turn",
        ),
    ]
    fake_client = _make_anthropic_with_responses(responses)

    async def _dispatch(name, input, csm_id):
        if name == "list_my_accounts":
            return {"accounts": ["Acme", "Beacon", "Sutton"]}
        if name == "get_calendar_events":
            return {
                "date": "2026-05-02",
                "customer_meetings": [],
                "internal_meetings": [],
                "total_customer_meetings": 0,
                "total_meetings": 2,
            }
        return {"error": "unknown tool"}

    fake_dispatch = AsyncMock(side_effect=_dispatch)

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        data = _post_chat("status update")

    assert fake_dispatch.await_count == 2
    called_tools = {call.args[0] for call in fake_dispatch.await_args_list}
    assert called_tools == {"list_my_accounts", "get_calendar_events"}
    # Second messages.create call must have received both tool_results
    second_call_kwargs = fake_client.messages.create.call_args_list[1].kwargs
    last_user_msg = second_call_kwargs["messages"][-1]
    assert last_user_msg["role"] == "user"
    assert isinstance(last_user_msg["content"], list)
    tool_result_ids = {b["tool_use_id"] for b in last_user_msg["content"]}
    assert tool_result_ids == {"tu_1", "tu_2"}


def test_chained_tools_across_iterations():
    """resolve_contact_email → find_threads_with_contact → text."""
    responses = [
        _make_response(
            [_tool_use_block("resolve_contact_email", {"name": "Marcus"}, "tu_1")],
            stop_reason="tool_use",
        ),
        _make_response(
            [_tool_use_block(
                "find_threads_with_contact",
                {"contact_email": "marcus@m.example"},
                "tu_2",
            )],
            stop_reason="tool_use",
        ),
        _make_response(
            [_text_block("Last email with Marcus was 3 days ago about the renewal.")],
            stop_reason="end_turn",
        ),
    ]
    fake_client = _make_anthropic_with_responses(responses)

    async def _dispatch(name, input, csm_id):
        if name == "resolve_contact_email":
            return {"matches": [{"name": "Marcus Reyes", "email": "marcus@m.example", "role": "VP", "account_name": "Meridian Health"}]}
        if name == "find_threads_with_contact":
            return {"contact_email": "marcus@m.example", "threads": [{"thread_id": "t1", "subject": "Renewal", "last_date": "2026-04-29"}]}
        return {"error": "unknown tool"}

    fake_dispatch = AsyncMock(side_effect=_dispatch)

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        data = _post_chat("emails with Marcus?")

    tools_in_order = [call.args[0] for call in fake_dispatch.await_args_list]
    assert tools_in_order == ["resolve_contact_email", "find_threads_with_contact"]
    assert "Marcus" in data["reply"] or "renewal" in data["reply"].lower()


def test_tool_dispatch_failure():
    """Tool raises; loop injects is_error tool_result and continues to text turn."""
    responses = [
        _make_response(
            [_tool_use_block("get_calendar_events", {}, "tu_1")],
            stop_reason="tool_use",
        ),
        _make_response(
            [_text_block("Calendar fetch failed. Try again in a moment.")],
            stop_reason="end_turn",
        ),
    ]
    fake_client = _make_anthropic_with_responses(responses)
    fake_dispatch = AsyncMock(side_effect=RuntimeError("calendar api down"))

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        data = _post_chat("today's meetings?")

    assert "reply" in data
    # Second messages.create must have an is_error tool_result in the user payload
    second_call_kwargs = fake_client.messages.create.call_args_list[1].kwargs
    last_user_msg = second_call_kwargs["messages"][-1]
    assert any(b.get("is_error") is True for b in last_user_msg["content"])


def test_max_iterations_hit():
    """Every messages.create returns tool_use; loop bails after MAX_ITERATIONS."""
    forever_tool_use = lambda: _make_response(
        [_tool_use_block("get_calendar_events", {}, "tu_x")],
        stop_reason="tool_use",
    )
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [forever_tool_use() for _ in range(20)]
    fake_dispatch = AsyncMock(return_value={"customer_meetings": [], "total_meetings": 0})

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        data = _post_chat("loop me forever")

    assert fake_client.messages.create.call_count == main._CHAT_TOOLUSE_MAX_ITERATIONS
    assert "reply" in data
    assert data["reply"]
    assert "more steps" in data["reply"] or "specific" in data["reply"]


def test_draft_card_in_final_text():
    """end_turn text with a DRAFT_CARD block is parsed into drafts[]."""
    canned_text = (
        "Drafted that for you.\n\n"
        "<<<DRAFT_CARD>>>\n"
        '{"label": "Reply — Marcus Reyes", "content": "Hi Marcus...", '
        '"action_type": "email_draft", '
        '"payload": {"to": "marcus@m.example", "subject": "Re: Q2 Renewal"}, '
        '"account_name": "Meridian Health"}\n'
        "<<<END_DRAFT_CARD>>>\n"
    )
    responses = [_make_response([_text_block(canned_text)], stop_reason="end_turn")]
    fake_client = _make_anthropic_with_responses(responses)
    fake_dispatch = AsyncMock()

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        data = _post_chat("draft a reply to Marcus")

    drafts = data.get("drafts", [])
    assert len(drafts) == 1
    assert drafts[0]["payload"]["to"] == "marcus@m.example"
    assert "<<<DRAFT_CARD>>>" not in data["reply"]
    assert "<<<END_DRAFT_CARD>>>" not in data["reply"]


def test_sarah_c_stays_on_legacy_when_flag_enabled():
    """sarah_c never routes to tooluse, even with the flag on. Verifies
    that messages.create was called WITHOUT tools= (legacy single-shot
    signature)."""
    canned = (
        "Quick status. "
        "<<<DRAFT_CARD>>>\n"
        '{"label": "Test", "content": "x", "action_type": "log_note", "payload": {}, "account_name": "Acme"}\n'
        "<<<END_DRAFT_CARD>>>\n"
    )
    fake_text = MagicMock()
    fake_text.text = canned
    fake_resp = MagicMock()
    fake_resp.content = [fake_text]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "read_signals", return_value=[]), \
         patch.object(main, "_anthropic_client", fake_client):
        response = client.post("/chat", json={"message": "status"})

    assert response.status_code == 200
    assert fake_client.messages.create.call_count == 1
    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert "tools" not in call_kwargs, (
        f"sarah_c must NOT receive tools= even when CHAT_TOOL_USE_ENABLED=true; got {list(call_kwargs.keys())!r}"
    )


def test_system_prompt_today_str_reflects_csm_timezone():
    """The 'Today is X' line in the system prompt must come from CSM tz,
    not UTC. Pacific CSM at 9pm local on May 3 (== May 4 UTC) sees May 3,
    so the LLM resolves 'tomorrow' against the right calendar day."""
    from datetime import datetime as _dt, timezone as _tz_module
    from zoneinfo import ZoneInfo

    fake_now_utc = _dt(2026, 5, 4, 4, 30, tzinfo=_tz_module.utc)

    class _FakeDateTime(_dt):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fake_now_utc.replace(tzinfo=None)
            return fake_now_utc.astimezone(tz)

    canned_text = "Sunday, May 03, 2026"
    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = canned_text
    fake_resp = MagicMock()
    fake_resp.content = [fake_text_block]
    fake_resp.stop_reason = "end_turn"
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_fake_session())), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "get_csm_timezone", return_value=ZoneInfo("America/Los_Angeles")), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch.object(main, "datetime", _FakeDateTime):
        response = client.post("/chat", json={"message": "what day is it?"})

    assert response.status_code == 200
    call_kwargs = fake_client.messages.create.call_args.kwargs
    system_blocks = call_kwargs["system"]
    system_text = system_blocks[0]["text"] if isinstance(system_blocks, list) else system_blocks
    # Pacific date is May 3, even though UTC is already May 4
    assert "May 03, 2026" in system_text
    # If today_str had used UTC, it would say "May 04, 2026" instead
    assert "May 04, 2026" not in system_text


def main_runner():
    """Standalone runner."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
