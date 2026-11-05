"""
tests/test_tooluse_zoom_capability.py

Regression guards for F-3: the tool-use chat system prompt must tell the LLM
whether Zoom is connected for the current user, so it stops defaulting to
"Zoom isn't connected" for users who have connected it.

Zoom recaps are NOT a callable tool — they arrive via webhook and surface
through get_account_dossier.recent_activity — so the capability is expressed
as guidance text, conditionally populated from load_zoom_token(csm_id), and
TOOL_DEFINITIONS is unchanged.

Standalone-runnable:
    python3 tests/test_tooluse_zoom_capability.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

import main


def _build(csm_id="user_x"):
    return main._build_tooluse_system_prompt("Jordan", "Monday, June 24, 2026", csm_id)


def test_zoom_connected_line_present_when_token_exists():
    with patch.object(main, "load_zoom_token", return_value={"access_token": "z", "expires_at": 9999999999}):
        prompt = _build()
    assert "Zoom is connected for this user" in prompt
    assert "recent_activity" in prompt
    assert "Zoom is not connected" not in prompt
    print("✓ connected: prompt states Zoom is connected, points to recent_activity")


def test_zoom_not_connected_line_present_when_no_token():
    with patch.object(main, "load_zoom_token", return_value=None):
        prompt = _build()
    assert "Zoom is not connected for this user" in prompt
    assert "connect it in Settings" in prompt
    assert "Zoom is connected for this user" not in prompt
    print("✓ not connected: prompt states Zoom is not connected, suggests Settings")


def test_zoom_token_check_exception_treated_as_not_connected():
    with patch.object(main, "load_zoom_token", side_effect=RuntimeError("db down")):
        prompt = _build()
    assert "Zoom is not connected for this user" in prompt
    print("✓ token-check exception falls back to not-connected, never raises")


def test_zoom_capability_is_guidance_not_a_tool():
    """The Zoom line must not be advertised as a callable tool in the tool
    list (it has no TOOL_DEFINITIONS entry)."""
    with patch.object(main, "load_zoom_token", return_value={"access_token": "z", "expires_at": 9999999999}):
        prompt = _build()
    # The tool list enumerates real tools with a trailing colon-description;
    # there must be no "Zoom:" tool entry mirroring "get_account_dossier:".
    assert "  - Zoom:" not in prompt
    assert "get_account_dossier:" in prompt
    print("✓ Zoom is guidance, not a tool-list entry")


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.run(["python3", "-m", "pytest", __file__, "-v"], cwd=str(REPO_ROOT)).returncode)
