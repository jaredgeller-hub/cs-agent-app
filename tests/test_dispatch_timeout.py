"""
tests/test_dispatch_timeout.py

Coverage for the asyncio.wait_for wrapper around dispatch_tool added in
commit 39bb13e (feat: wrap dispatch_tool in asyncio.wait_for with 30s
default).

The existing test_tool_dispatch_failure (test_chat_tooluse_loop.py:221)
covers the call-site path that wraps unhandled RuntimeError into an
is_error: True envelope. This file tests the dispatch-level timeout
shaping that lives below it: structured error dict, no exception
escape, per-tool override, normal-path passthrough, request_id
correlation, and unknown-tool ValueError preservation.

Tests use asyncio.run inside sync test functions, matching the
convention in tests/test_chat_tool_*.py (no pytest-asyncio dep).

Standalone-runnable:
    python3 tests/test_dispatch_timeout.py
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.chat_tools import registry
from product.chat_tools.registry import dispatch_tool, PER_TOOL_TIMEOUTS_S
from product.logging_config import request_id_var


# ── timeout shape ────────────────────────────────────────────────────────────


def test_dispatch_tool_timeout_returns_structured_error():
    """Timeout returns a dict with error string and timed_out: True; no exception escapes."""

    async def slow_inner(name, input, csm_id):
        await asyncio.sleep(60)
        return {"never": "returned"}

    with patch.object(registry, "_dispatch_inner", side_effect=slow_inner), \
         patch.dict(PER_TOOL_TIMEOUTS_S, {"list_my_accounts": 0.1}, clear=False):
        result = asyncio.run(dispatch_tool("list_my_accounts", {}, "test-csm"))

    assert isinstance(result, dict)
    assert result.get("timed_out") is True
    assert "list_my_accounts" in result.get("error", "")
    # f"{0.1:.0f}s" rounds to "0s"; the wrapper's format string uses %.0f
    assert "s" in result.get("error", "")


def test_dispatch_tool_timeout_does_not_propagate():
    """No asyncio.TimeoutError or other exception escapes dispatch_tool on timeout."""

    async def slow_inner(name, input, csm_id):
        await asyncio.sleep(60)

    with patch.object(registry, "_dispatch_inner", side_effect=slow_inner), \
         patch.dict(PER_TOOL_TIMEOUTS_S, {"list_my_accounts": 0.1}, clear=False):
        result = asyncio.run(dispatch_tool("list_my_accounts", {}, "test-csm"))

    assert isinstance(result, dict)


# ── per-tool override ────────────────────────────────────────────────────────


def test_dispatch_tool_per_tool_override_honored():
    """PER_TOOL_TIMEOUTS_S override fires before DEFAULT_TOOL_TIMEOUT_S."""

    async def slow_inner(name, input, csm_id):
        await asyncio.sleep(60)

    async def runner():
        loop = asyncio.get_event_loop()
        start = loop.time()
        result = await dispatch_tool("get_calendar_events", {}, "test-csm")
        elapsed = loop.time() - start
        return result, elapsed

    with patch.object(registry, "_dispatch_inner", side_effect=slow_inner), \
         patch.dict(PER_TOOL_TIMEOUTS_S, {"get_calendar_events": 0.05}, clear=False):
        result, elapsed = asyncio.run(runner())

    assert result.get("timed_out") is True
    # 0.05s override fired well before any 30s default would have
    assert elapsed < 1.0


# ── normal path is unchanged ─────────────────────────────────────────────────


def test_dispatch_tool_succeeds_under_timeout():
    """Normal completion path is unchanged; result passes through untouched."""

    async def fast_inner(name, input, csm_id):
        return {"success": True, "data": [1, 2, 3]}

    with patch.object(registry, "_dispatch_inner", side_effect=fast_inner):
        result = asyncio.run(dispatch_tool("list_my_accounts", {}, "test-csm"))

    assert result == {"success": True, "data": [1, 2, 3]}
    assert "timed_out" not in result


# ── request_id correlation ───────────────────────────────────────────────────


def test_dispatch_tool_timeout_error_includes_request_id():
    """request_id from contextvar must appear in the LLM-visible error string.

    The contextvar set/reset pattern (rather than direct assignment) is
    required so the test does not bleed across parallel runs.
    """

    async def slow_inner(name, input, csm_id):
        await asyncio.sleep(60)

    test_rid = "abc-123-test-correlation"

    async def runner():
        token = request_id_var.set(test_rid)
        try:
            return await dispatch_tool("list_my_accounts", {}, "test-csm")
        finally:
            request_id_var.reset(token)

    with patch.object(registry, "_dispatch_inner", side_effect=slow_inner), \
         patch.dict(PER_TOOL_TIMEOUTS_S, {"list_my_accounts": 0.1}, clear=False):
        result = asyncio.run(runner())

    assert test_rid in result.get("error", "")


# ── unknown-tool ValueError stays outside the wrapper ────────────────────────


def test_dispatch_tool_unknown_tool_still_raises_value_error():
    """Unknown tool name remains a loud-fail ValueError, not a wrapped timeout dict.

    Unknown-tool is a programmer error in the chat handler. The existing
    main.py:7178-7192 catch-all surfaces it as is_error: True at the
    call site, which is the correct loud-fail behavior.
    """
    with pytest.raises(ValueError, match="unknown tool"):
        asyncio.run(dispatch_tool("not_a_real_tool", {}, "test-csm"))


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v", "--timeout=30"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
