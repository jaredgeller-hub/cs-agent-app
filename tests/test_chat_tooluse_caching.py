"""
tests/test_chat_tooluse_caching.py

Verifies Anthropic prompt caching is wired correctly in the tool-use
chat handler. Two cache_control markers should be sent:
  1. cache_control on the system prompt block (added in Commit 8)
  2. cache_control on the last tool definition (added in Commit 1)

These markers cause Anthropic to cache the [tools + system] prefix so
subsequent turns within the 5-minute window read it at 0.1x input cost.

The tests don't verify Anthropic's caching behavior itself; that's
out of scope. They verify the wire-format is correct so the cache
engages on the API's side.

Standalone-runnable:
    python3 tests/test_chat_tooluse_caching.py
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
from product.chat_tools.registry import get_tools_with_cache_control

client = TestClient(main.app)


def _text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_response(content_blocks: list, stop_reason: str = "end_turn") -> MagicMock:
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    resp.usage = MagicMock(
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_tokens=0,
        output_tokens=0,
    )
    return resp


def _trialing_session_factory() -> MagicMock:
    fake_row = MagicMock()
    fake_row.subscription_status = "trialing"
    fake_session = MagicMock()
    fake_session.query.return_value.filter_by.return_value.first.return_value = fake_row
    return MagicMock(return_value=fake_session)


_FAKE_SESSION = {"csm_id": "user_test123", "name": "Test User", "email": "test@example.com"}


def test_system_prompt_uses_cache_control():
    """The system kwarg must be a list containing one block with cache_control."""
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_response(
        [_text_block("ok")], stop_reason="end_turn",
    )

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client):
        resp = client.post("/chat", json={"message": "hello"})

    assert resp.status_code == 200
    assert fake_client.messages.create.call_count == 1
    kwargs = fake_client.messages.create.call_args.kwargs

    system = kwargs.get("system")
    assert isinstance(system, list), f"expected system to be a list, got {type(system).__name__}"
    assert len(system) == 1, f"expected exactly one system block, got {len(system)}"

    block = system[0]
    assert block.get("type") == "text"
    assert block.get("cache_control") == {"type": "ephemeral"}
    assert isinstance(block.get("text"), str) and len(block["text"]) > 100


def test_tools_list_has_cache_control_on_last_entry_only():
    """get_tools_with_cache_control marks only the last tool, leaving
    TOOL_DEFINITIONS itself unmutated. This ensures Anthropic caches
    the entire tools array as one cacheable prefix."""
    tools = get_tools_with_cache_control()
    assert len(tools) >= 1
    assert tools[-1].get("cache_control") == {"type": "ephemeral"}
    for t in tools[:-1]:
        assert "cache_control" not in t, (
            f"only the last tool should have cache_control, found one on {t.get('name')!r}"
        )


def test_messages_create_actually_receives_cached_tools():
    """The tools kwarg passed to messages.create must include a tool with
    cache_control so the [tools + system] prefix caches as one block."""
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_response(
        [_text_block("ok")], stop_reason="end_turn",
    )

    with patch.dict(os.environ, {"CHAT_TOOL_USE_ENABLED": "true"}), \
         patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_FAKE_SESSION)), \
         patch("product.database.SessionLocal", _trialing_session_factory()), \
         patch.object(main, "_anthropic_client", fake_client):
        resp = client.post("/chat", json={"message": "hi"})

    assert resp.status_code == 200
    kwargs = fake_client.messages.create.call_args.kwargs
    tools = kwargs.get("tools") or []
    assert any("cache_control" in t for t in tools), (
        "tools list passed to messages.create must include cache_control on at least one tool"
    )


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
