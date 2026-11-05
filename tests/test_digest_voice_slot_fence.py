"""
tests/test_digest_voice_slot_fence.py

Regression for bug 2 (June 3 canary): _generate_voice_slots fed the raw
model text straight into json.loads after only .strip(). When the model
wrapped its JSON in a ```json ... ``` markdown fence, json.loads failed
at char 0 and the except branch returned EMPTY voice slots — silently
dropping the priority_line and closing_line from every digest.

The fix adds _strip_code_fence before json.loads. These tests exercise
the REAL _generate_voice_slots with a stubbed Anthropic client (no
network, no live Anthropic), plus a direct unit test of _strip_code_fence.

NOTE: this file deliberately does NOT use the suite-wide monkeypatch of
_generate_voice_slots (test_digest_generator.py does that). We need the
real function body to prove the fence is stripped.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.digest import generator as gen


class _StubMessage:
    def __init__(self, text: str):
        self.content = [type("Block", (), {"text": text})()]


class _StubClient:
    def __init__(self, text: str):
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        return _StubMessage(self._text)


def _run_voice_slots(monkeypatch, text: str) -> dict:
    monkeypatch.setattr(gen, "_get_client", lambda: _StubClient(text))
    return gen._generate_voice_slots(
        csm_name="Sarah",
        tier_a=[{"account": "Northgate Logistics"}],
        tier_b=[],
        crm_type="hubspot",
    )


def test_voice_slots_parse_fenced_json(monkeypatch):
    """Case 1: ```json ... ``` fence — both lines parse non-empty."""
    text = (
        '```json\n'
        '{"priority_line": "Northgate Logistics needs a touch today.", '
        '"closing_line": "Start there, then clear tickets."}\n'
        '```'
    )
    result = _run_voice_slots(monkeypatch, text)
    assert result["priority_line"] == "Northgate Logistics needs a touch today."
    assert result["closing_line"] == "Start there, then clear tickets."
    print("✓ case 1: fenced ```json``` parses to non-empty lines")


def test_voice_slots_parse_bare_fence(monkeypatch):
    """Case 2: bare ``` ... ``` fence — parses non-empty."""
    text = '```\n{"priority_line":"x","closing_line":"y"}\n```'
    result = _run_voice_slots(monkeypatch, text)
    assert result["priority_line"] == "x"
    assert result["closing_line"] == "y"
    print("✓ case 2: bare fence parses to non-empty lines")


def test_voice_slots_parse_no_fence(monkeypatch):
    """Case 3: no fence — unchanged behavior, still parses."""
    text = '{"priority_line":"x","closing_line":"y"}'
    result = _run_voice_slots(monkeypatch, text)
    assert result["priority_line"] == "x"
    assert result["closing_line"] == "y"
    print("✓ case 3: unfenced JSON still parses")


def test_strip_code_fence_shapes():
    """Direct unit test of _strip_code_fence across all shapes."""
    fenced_json = '```json\n{"a": 1}\n```'
    assert gen._strip_code_fence(fenced_json) == '{"a": 1}'

    bare_fence = '```\n{"a": 1}\n```'
    assert gen._strip_code_fence(bare_fence) == '{"a": 1}'

    no_fence = '{"a": 1}'
    assert gen._strip_code_fence(no_fence) == '{"a": 1}'

    assert gen._strip_code_fence("") == ""
    assert gen._strip_code_fence("plain text") == "plain text"
    print("✓ _strip_code_fence: fenced/bare/none/empty/plain all correct")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
