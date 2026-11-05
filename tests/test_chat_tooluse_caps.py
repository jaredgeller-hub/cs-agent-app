"""
tests/test_chat_tooluse_caps.py

Tests for the operational safeguards on the tool-use chat handler:
  - Token budget cap across iterations breaks the loop with a degraded reply
  - Tool-call-per-iteration cap dispatches at most N tools regardless of
    how many the LLM requested
  - Per-tool latency timing is logged at INFO level

Standalone-runnable:
    python3 tests/test_chat_tooluse_caps.py
"""

import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────


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


def _make_response(content_blocks: list, stop_reason: str = "end_turn",
                   input_tokens: int = 0, output_tokens: int = 0) -> MagicMock:
    """Mock anthropic response. Setting input/output_tokens lets a test
    drive cumulative_tokens past the budget cap deterministically."""
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    resp.usage = MagicMock(
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return resp


def _trialing_session_factory() -> MagicMock:
    fake_row = MagicMock()
    fake_row.subscription_status = "trialing"
    fake_session = MagicMock()
    fake_session.query.return_value.filter_by.return_value.first.return_value = fake_row
    return MagicMock(return_value=fake_session)


_FAKE_SESSION = {"csm_id": "user_test123", "name": "Test User", "email": "test@example.com"}


# ── Tests ────────────────────────────────────────────────────────────────────


def test_token_budget_cap_triggers_break():
    """Each iteration reports tokens that sum past the budget by iteration 2.
    The handler must exit with a degraded reply rather than continuing."""
    big_iter_tokens = main._CHAT_TOOLUSE_TOKEN_BUDGET // 2 + 5000  # tip past on iter 2
    responses = [
        _make_response(
            [_tool_use_block("get_calendar_events", {}, f"tu_{i}")],
            stop_reason="tool_use",
            input_tokens=big_iter_tokens,
            output_tokens=0,
        )
        for i in range(5)
    ]
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = responses

    fake_dispatch = AsyncMock(return_value={"customer_meetings": [], "total_meetings": 0})

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        resp = client.post("/chat", json={"message": "test budget"})

    assert resp.status_code == 200
    data = resp.json()
    # The loop should bail before consuming all 4 iterations. Iter 1 puts
    # cumulative just over budget/2; iter 2 trips it. Allow up to iter 3.
    assert fake_client.messages.create.call_count <= 3
    assert "ran out of room" in data["reply"] or "specific" in data["reply"]


def test_max_tools_per_iteration_caps_dispatch():
    """LLM requests 10 tool_use blocks in one assistant turn; only the
    first _CHAT_TOOLUSE_MAX_TOOLS_PER_ITERATION are dispatched."""
    requested = 10
    cap = main._CHAT_TOOLUSE_MAX_TOOLS_PER_ITERATION
    assert requested > cap, "test premise requires requested > cap"

    blocks = [
        _tool_use_block("get_calendar_events", {}, f"tu_{i}")
        for i in range(requested)
    ]
    responses = [
        _make_response(blocks, stop_reason="tool_use"),
        _make_response([_text_block("done")], stop_reason="end_turn"),
    ]
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = responses

    fake_dispatch = AsyncMock(return_value={"customer_meetings": [], "total_meetings": 0})

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        resp = client.post("/chat", json={"message": "fan out"})

    assert resp.status_code == 200
    assert fake_dispatch.await_count == cap


def test_per_tool_latency_logged(caplog):
    """A successful dispatch_tool call emits an INFO log with tool=<name>
    and duration_ms=<n>. Captured via pytest's caplog fixture."""
    responses = [
        _make_response(
            [_tool_use_block("get_calendar_events", {}, "tu_1")],
            stop_reason="tool_use",
        ),
        _make_response([_text_block("ok")], stop_reason="end_turn"),
    ]
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = responses

    async def _slow_dispatch(name, input, csm_id):
        # Sleep just enough that duration_ms is provably non-zero.
        await __import__("asyncio").sleep(0.005)
        return {"customer_meetings": [], "total_meetings": 0}

    fake_dispatch = AsyncMock(side_effect=_slow_dispatch)

    with caplog.at_level(logging.INFO, logger="cs-agent"), \
         patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        resp = client.post("/chat", json={"message": "time it"})

    assert resp.status_code == 200
    matches = [
        r for r in caplog.records
        if "tool=get_calendar_events" in r.getMessage() and "duration_ms=" in r.getMessage()
    ]
    assert matches, f"expected per-tool latency log, got records: {[r.getMessage() for r in caplog.records]!r}"


def test_per_tool_latency_logged_on_failure(caplog):
    """A failing dispatch_tool emits a WARNING log with the same fields
    plus an error string."""
    responses = [
        _make_response(
            [_tool_use_block("get_calendar_events", {}, "tu_1")],
            stop_reason="tool_use",
        ),
        _make_response([_text_block("recovered")], stop_reason="end_turn"),
    ]
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = responses
    fake_dispatch = AsyncMock(side_effect=RuntimeError("calendar api down"))

    with caplog.at_level(logging.WARNING, logger="cs-agent"), \
         patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client), \
         patch("product.chat_tools.registry.dispatch_tool", fake_dispatch):
        resp = client.post("/chat", json={"message": "fail it"})

    assert resp.status_code == 200
    matches = [
        r for r in caplog.records
        if "tool=get_calendar_events" in r.getMessage()
        and "FAILED" in r.getMessage()
        and "calendar api down" in r.getMessage()
    ]
    assert matches, f"expected per-tool failure log, got records: {[r.getMessage() for r in caplog.records]!r}"


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
