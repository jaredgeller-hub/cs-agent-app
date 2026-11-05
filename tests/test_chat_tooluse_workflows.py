"""
tests/test_chat_tooluse_workflows.py

End-to-end workflow tests for the /chat tool-use handler. Each test
mocks the Anthropic client to simulate the LLM picking the right tool
sequence for a given user prompt, mocks dispatch_tool to return realistic
fixture data, and asserts that the final reply (or drafts[]) contains
the relevant facts.

Like test_chat_tooluse_loop.py, these tests verify the pipe and not
Claude's actual decision-making. They prove the wiring carries data
through correctly when the LLM cooperates.

Standalone-runnable:
    python3 tests/test_chat_tooluse_workflows.py
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


# ── Mock builders (small duplicate to keep this file self-contained) ─────────


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
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


def _make_anthropic(responses: list) -> MagicMock:
    fake = MagicMock()
    fake.messages.create.side_effect = list(responses)
    return fake


def _trialing_session_factory() -> MagicMock:
    fake_row = MagicMock()
    fake_row.subscription_status = "trialing"
    fake_session = MagicMock()
    fake_session.query.return_value.filter_by.return_value.first.return_value = fake_row
    return MagicMock(return_value=fake_session)


_FAKE_SESSION = {"csm_id": "user_test123", "name": "Test User", "email": "test@example.com"}


# ── Workflow tests ───────────────────────────────────────────────────────────


def test_workflow_calendar_today():
    """User asks about today's meetings; LLM calls get_calendar_events."""
    responses = [
        _make_response(
            [_tool_use_block("get_calendar_events", {}, "tu_1")],
            stop_reason="tool_use",
        ),
        _make_response(
            [_text_block("You have 2 customer meetings: Acme QBR at 10am and Beacon sync at 2pm.")],
            stop_reason="end_turn",
        ),
    ]
    fake_client = _make_anthropic(responses)
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
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        resp = client.post("/chat", json={"message": "what meetings do I have today?"})

    assert resp.status_code == 200
    data = resp.json()
    assert "Acme" in data["reply"]
    assert "Beacon" in data["reply"]
    assert fake_dispatch.await_count == 1
    assert fake_dispatch.await_args.args[0] == "get_calendar_events"


def test_workflow_emails_from_marcus():
    """User asks about emails from Marcus; LLM resolves contact then fetches threads."""
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
            [_text_block(
                "Three recent threads with Marcus Reyes: Renewal kickoff (3 days ago), "
                "Q2 review notes, and Onboarding follow-up."
            )],
            stop_reason="end_turn",
        ),
    ]
    fake_client = _make_anthropic(responses)

    async def _dispatch(name, input, csm_id):
        if name == "resolve_contact_email":
            return {"matches": [{"name": "Marcus Reyes", "email": "marcus@m.example", "role": "VP", "account_name": "Meridian Health"}]}
        if name == "find_threads_with_contact":
            return {
                "contact_email": "marcus@m.example",
                "threads": [
                    {"thread_id": "t1", "subject": "Renewal kickoff", "last_date": "2026-04-29", "direction": "received"},
                    {"thread_id": "t2", "subject": "Q2 review notes", "last_date": "2026-04-25", "direction": "sent"},
                    {"thread_id": "t3", "subject": "Onboarding follow-up", "last_date": "2026-04-15", "direction": "received"},
                ],
            }
        return {"error": "unknown"}

    fake_dispatch = AsyncMock(side_effect=_dispatch)

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        resp = client.post("/chat", json={"message": "summarize emails from Marcus Reyes"})

    assert resp.status_code == 200
    data = resp.json()
    chain = [c.args[0] for c in fake_dispatch.await_args_list]
    assert chain == ["resolve_contact_email", "find_threads_with_contact"]
    assert "Marcus" in data["reply"] or "Renewal" in data["reply"]


def test_workflow_draft_email_to_marcus():
    """User asks for a draft email; reply includes a DRAFT_CARD with correct `to`."""
    canned_final = (
        "Drafted that email for you.\n\n"
        "<<<DRAFT_CARD>>>\n"
        '{"label": "Email — Marcus Reyes", "content": "Hi Marcus,\\n\\nWanted to align on QBR timing.", '
        '"action_type": "email_draft", '
        '"payload": {"to": "marcus@m.example", "subject": "QBR alignment"}, '
        '"account_name": "Meridian Health"}\n'
        "<<<END_DRAFT_CARD>>>\n"
    )
    responses = [
        _make_response(
            [_tool_use_block("resolve_contact_email", {"name": "Marcus"}, "tu_1")],
            stop_reason="tool_use",
        ),
        _make_response([_text_block(canned_final)], stop_reason="end_turn"),
    ]
    fake_client = _make_anthropic(responses)
    fake_dispatch = AsyncMock(return_value={
        "matches": [{"name": "Marcus Reyes", "email": "marcus@m.example", "role": "VP", "account_name": "Meridian Health"}]
    })

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        resp = client.post("/chat", json={"message": "draft an email to Marcus about QBR"})

    assert resp.status_code == 200
    data = resp.json()
    drafts = data.get("drafts", [])
    assert len(drafts) == 1
    assert drafts[0]["payload"]["to"] == "marcus@m.example"
    assert drafts[0]["action_type"] == "email_draft"


def test_workflow_meridian_dossier():
    """User asks about Meridian; LLM calls get_account_dossier."""
    responses = [
        _make_response(
            [_tool_use_block("get_account_dossier", {"account_name": "Meridian Health"}, "tu_1")],
            stop_reason="tool_use",
        ),
        _make_response(
            [_text_block(
                "Meridian Health: 1 contact (Marcus Reyes, VP), 1 deal in Closed Won "
                "($85k), 1 open ticket. Last activity was a kickoff meeting 3 days ago."
            )],
            stop_reason="end_turn",
        ),
    ]
    fake_client = _make_anthropic(responses)
    fake_dispatch = AsyncMock(return_value={
        "account": {"name": "Meridian Health", "id": 123, "domain": "meridian.example", "industry": "Healthcare", "owner_id": ""},
        "contacts": [{"name": "Marcus Reyes", "email": "marcus@m.example", "role": "VP"}],
        "deals": [{"name": "Renewal", "stage": "Closed Won", "amount": 85000, "close_date": "2026-08-01"}],
        "tickets": [{"subject": "QBR follow-up", "status": "OPEN", "priority": "HIGH"}],
        "recent_activity": [{"type": "meeting", "summary": "Kickoff", "date": "2026-04-29"}],
    })

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        resp = client.post("/chat", json={"message": "what's happening with Meridian Health?"})

    assert resp.status_code == 200
    data = resp.json()
    assert fake_dispatch.await_count == 1
    assert fake_dispatch.await_args.args[0] == "get_account_dossier"
    assert fake_dispatch.await_args.args[1]["account_name"] == "Meridian Health"
    reply_lower = data["reply"].lower()
    assert "marcus" in reply_lower or "meridian" in reply_lower


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
