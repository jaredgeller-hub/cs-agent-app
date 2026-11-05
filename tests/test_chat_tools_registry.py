"""
tests/test_chat_tools_registry.py

Structural tests for product/chat_tools/registry.py. Verifies:
  - All five tools are present and uniquely named.
  - Each tool's input_schema is a structurally valid JSON Schema (object
    type, properties dict, required list, additionalProperties boolean).
  - dispatch_tool raises NotImplementedError for known tools (stub state)
    and ValueError for unknown tool names.
  - get_tools_with_cache_control returns a copy with cache_control on the
    last entry only, leaving TOOL_DEFINITIONS unmutated.

These are pure unit tests. No LLM, no extractors, no DB.

Standalone-runnable:
    python3 tests/test_chat_tools_registry.py
"""

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.chat_tools.registry import (
    TOOL_DEFINITIONS,
    dispatch_tool,
    get_tools_with_cache_control,
)


EXPECTED_TOOL_NAMES = {
    "get_calendar_events",
    "list_my_accounts",
    "get_account_dossier",
    "resolve_contact_email",
    "find_threads_with_contact",
    "find_recent_transcripts",
    "read_transcript_excerpt",
    "slack_draft_message",
}


def test_tool_definitions_count_and_unique_names():
    """All eight tools present, names unique."""
    assert len(TOOL_DEFINITIONS) == 8
    names = [t["name"] for t in TOOL_DEFINITIONS]
    assert len(set(names)) == 8
    assert set(names) == EXPECTED_TOOL_NAMES


def test_input_schemas_are_structurally_valid():
    """Each tool has a JSON Schema shaped input_schema."""
    for tool in TOOL_DEFINITIONS:
        assert "name" in tool
        assert "description" in tool
        assert isinstance(tool["description"], str) and len(tool["description"]) > 20
        schema = tool.get("input_schema")
        assert isinstance(schema, dict), f"{tool['name']}: input_schema missing"
        assert schema.get("type") == "object"
        assert isinstance(schema.get("properties"), dict)
        assert isinstance(schema.get("required"), list)
        assert schema.get("additionalProperties") is False
        # Required keys must exist in properties
        for required_key in schema["required"]:
            assert required_key in schema["properties"], (
                f"{tool['name']}: required key {required_key} missing from properties"
            )


# Tools wired so far (subsequent commits will move names from STUB to WIRED).
WIRED_TOOL_NAMES = {
    "get_calendar_events",
    "list_my_accounts",
    "resolve_contact_email",
    "find_threads_with_contact",
    "get_account_dossier",
    "find_recent_transcripts",
    "read_transcript_excerpt",
    "slack_draft_message",
}
STUB_TOOL_NAMES = EXPECTED_TOOL_NAMES - WIRED_TOOL_NAMES


def test_dispatch_tool_raises_not_implemented_for_stubbed_tool():
    """Stubbed tools still raise NotImplementedError until they are wired."""
    if not STUB_TOOL_NAMES:
        pytest.skip("all tools wired; no stubs left to verify")
    sample_stubbed = next(iter(STUB_TOOL_NAMES))
    with pytest.raises(NotImplementedError):
        asyncio.run(dispatch_tool(sample_stubbed, {}, "sarah_c"))


def test_dispatch_tool_raises_value_error_for_unknown_tool():
    """Unknown tool name raises ValueError, not NotImplementedError."""
    with pytest.raises(ValueError):
        asyncio.run(dispatch_tool("not_a_real_tool", {}, "sarah_c"))


def test_dispatch_tool_raises_not_implemented_for_every_stubbed_tool():
    """All currently-stubbed tools raise NotImplementedError. The wired set
    has its own coverage in dedicated test files (e.g. test_chat_tool_calendar.py)."""
    if not STUB_TOOL_NAMES:
        pytest.skip("all tools wired; no stubs left to verify")
    for name in STUB_TOOL_NAMES:
        with pytest.raises(NotImplementedError):
            asyncio.run(dispatch_tool(name, {}, "sarah_c"))


def test_get_tools_with_cache_control_marks_last_entry_only():
    """cache_control is set on the last tool, no others."""
    tools = get_tools_with_cache_control()
    assert len(tools) == len(TOOL_DEFINITIONS)
    # Last entry has cache_control
    assert tools[-1].get("cache_control") == {"type": "ephemeral"}
    # All other entries do not
    for t in tools[:-1]:
        assert "cache_control" not in t


def test_cache_control_marker_lands_on_last_tool():
    """slack_draft_message is appended last, so the cache_control marker
    lands there. The marker only needs to sit on the final tool for prompt
    caching; the specific tool is not significant."""
    tools = get_tools_with_cache_control()
    assert tools[-1]["name"] == "slack_draft_message"
    assert tools[-1].get("cache_control") == {"type": "ephemeral"}


def test_get_tools_with_cache_control_does_not_mutate_original():
    """TOOL_DEFINITIONS is left untouched after the call."""
    _ = get_tools_with_cache_control()
    for t in TOOL_DEFINITIONS:
        assert "cache_control" not in t


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
