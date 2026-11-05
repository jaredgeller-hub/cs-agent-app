"""
tests/test_oauth_postmessage.py

Regression guard for the OAuth callback popup-side postMessage targetOrigin.

The three OAuth callback handlers in main.py serve inline HTML that calls
window.opener.postMessage(...) to signal the parent onboarding tab. The
target origin MUST be window.location.origin (same-origin) — never "*"
(any origin), which would let any opener page read the success payload.

The fix landed Apr 16 in commit f9225ee. This test scans the live source
of main.py and asserts the post-fix invariant on every postMessage call
inside an OAuth callback. If anyone copy-pastes "*" into the callback
HTML in the future, this test fails with a clear pointer to the line.

Pair test (listener side): frontend/tests/postmessage_listener.test.jsx.

Structured for Day 3 CI promotion. Standalone-runnable today:
    python3 tests/test_oauth_postmessage.py
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = REPO_ROOT / "main.py"

PROVIDERS = ["google", "hubspot", "gainsight"]


def _read_main_py() -> str:
    return MAIN_PY.read_text(encoding="utf-8")


def _find_postmessage_calls() -> list[tuple[int, str]]:
    """Return [(line_number, full_line), ...] for every postMessage call in main.py."""
    out = []
    for i, line in enumerate(_read_main_py().splitlines(), start=1):
        # Scope to actual browser postMessage calls (the popup -> opener
        # signal), which are always window.opener.postMessage(...). This
        # deliberately excludes unrelated identifiers that merely contain
        # the substring "postMessage" - e.g. Slack's Web API method name
        # chat.postMessage, which is an HTTP endpoint, not a window call.
        if "opener.postMessage" in line:
            out.append((i, line))
    return out


def test_postmessage_calls_exist():
    """Sanity: at least 3 postMessage calls (one per provider)."""
    calls = _find_postmessage_calls()
    assert len(calls) >= 3, f"expected >=3 postMessage calls, found {len(calls)}: {calls}"
    print(f"✓ found {len(calls)} postMessage calls in main.py")


def test_no_wildcard_target_origin():
    """No postMessage call may use '*' as the target origin."""
    calls = _find_postmessage_calls()
    offenders = []
    for ln, line in calls:
        # Match patterns like: postMessage({...}, "*")  or  postMessage(payload, '*')
        if re.search(r'postMessage\s*\([^)]*,\s*[\'"]\*[\'"]\s*\)', line):
            offenders.append((ln, line.strip()))
    assert not offenders, (
        "postMessage calls using '*' target origin (this is the bug fixed in f9225ee):\n  "
        + "\n  ".join(f"main.py:{ln}: {text}" for ln, text in offenders)
    )
    print(f"✓ no postMessage call uses '*' as target origin")


def test_all_postmessage_calls_use_window_location_origin():
    """Every postMessage call must use window.location.origin as the target."""
    calls = _find_postmessage_calls()
    bad = []
    for ln, line in calls:
        if not re.search(r'postMessage\s*\([^)]*,\s*window\.location\.origin\s*\)', line):
            bad.append((ln, line.strip()))
    assert not bad, (
        "postMessage calls missing window.location.origin target:\n  "
        + "\n  ".join(f"main.py:{ln}: {text}" for ln, text in bad)
    )
    print(f"✓ all {len(calls)} postMessage calls use window.location.origin")


def test_each_provider_has_a_postmessage_call():
    """Each provider's callback HTML must include a postMessage with provider:'<name>'."""
    src = _read_main_py()
    missing = []
    for p in PROVIDERS:
        pattern = re.compile(
            r'postMessage\s*\(\s*\{[^}]*provider:\s*[\'"]' + re.escape(p) + r'[\'"][^}]*\}'
        )
        if not pattern.search(src):
            missing.append(p)
    assert not missing, f"no postMessage call found for providers: {missing}"
    print(f"✓ all 3 providers have a postMessage call: {PROVIDERS}")


def test_each_provider_postmessage_uses_safe_target():
    """Belt-and-suspenders: assert per-provider that the line is safe."""
    src = _read_main_py().splitlines()
    failures = []
    for p in PROVIDERS:
        # Find the line containing this provider's postMessage call
        provider_lines = [
            (i, ln) for i, ln in enumerate(src, start=1)
            if "postMessage" in ln and f'provider: "{p}"' in ln
        ]
        if not provider_lines:
            failures.append(f"{p}: no postMessage line found")
            continue
        for ln_num, ln in provider_lines:
            if "window.location.origin" not in ln:
                failures.append(f"{p} (main.py:{ln_num}): missing window.location.origin")
            if re.search(r',\s*[\'"]\*[\'"]\s*\)', ln):
                failures.append(f"{p} (main.py:{ln_num}): uses '*' target")
    assert not failures, "\n  ".join([""] + failures)
    print(f"✓ each provider's postMessage line uses safe target")


def main():
    tests = [
        test_postmessage_calls_exist,
        test_no_wildcard_target_origin,
        test_all_postmessage_calls_use_window_location_origin,
        test_each_provider_has_a_postmessage_call,
        test_each_provider_postmessage_uses_safe_target,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
            failures.append(t.__name__)
        except Exception as e:
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
            failures.append(t.__name__)
    print()
    if failures:
        print(f"FAILED: {len(failures)} of {len(tests)}")
        for n in failures:
            print(f"  - {n}")
        sys.exit(1)
    print(f"PASSED: {len(tests)} of {len(tests)}")


if __name__ == "__main__":
    main()
