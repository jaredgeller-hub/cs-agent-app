"""
tests/test_csm_id_validation.py

Verifies product.csm_id_validation rejects path traversal attempts and
accepts legitimate Clerk-style and legacy csm_ids. Also verifies the
safe_data_path containment check catches escapes that the regex doesn't
(belt-and-suspenders).

Structured as standalone-runnable today; can be promoted to pytest in
Day 3's CI work without restructuring.

Usage:
    python3 tests/test_csm_id_validation.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi import HTTPException

from product.csm_id_validation import (
    validate_csm_id,
    safe_data_path,
    safe_data_file,
)


REJECT_CASES = [
    ("..", "bare double-dot"),
    (".", "bare dot"),
    ("../etc/passwd", "classic traversal"),
    ("foo/../bar", "embedded traversal"),
    ("foo..bar", "embedded double-dot"),
    (".hidden", "leading dot (hidden file)"),
    ("trailing.", "trailing dot"),
    ("", "empty string"),
    ("a" * 65, "65-char string (length bound)"),
    ("foo\x00bar", "null byte injection"),
    ("foo/bar", "forward slash"),
    ("foo\\bar", "backslash"),
    ("foo bar", "space"),
    ("foo;rm -rf /", "shell metacharacter"),
    ("foo$bar", "dollar sign"),
    ("a" * 100, "way too long"),
]

ACCEPT_CASES = [
    "sarah_c",
    "sarah.c",
    "user_3FixtureClerkFormat00000000",
    "a",
    "a-b-c",
    "Mixed_Case-123",
    "x" * 64,  # exactly at the length bound
]


def test_rejects_bad_inputs():
    failures = []
    for bad, label in REJECT_CASES:
        try:
            result = validate_csm_id(bad)
            failures.append(f"{label!r}: accepted (returned {result!r}) — should have raised")
        except HTTPException as e:
            if e.status_code != 400:
                failures.append(f"{label!r}: raised HTTP {e.status_code}, expected 400")
    if failures:
        raise AssertionError("REJECT failures:\n  " + "\n  ".join(failures))
    print(f"✓ rejected {len(REJECT_CASES)} bad inputs")


def test_accepts_good_inputs():
    failures = []
    for good in ACCEPT_CASES:
        try:
            r = validate_csm_id(good)
            if r != good:
                failures.append(f"{good!r}: returned {r!r}, expected unchanged")
        except HTTPException as e:
            failures.append(f"{good!r}: rejected with HTTP {e.status_code} ({e.detail})")
    if failures:
        raise AssertionError("ACCEPT failures:\n  " + "\n  ".join(failures))
    print(f"✓ accepted {len(ACCEPT_CASES)} good inputs")


def test_safe_data_path_basic_construction():
    p = safe_data_path("product/data/digests", "sarah_c", "2026-04-18.json")
    assert p.name == "2026-04-18.json", f"unexpected filename: {p.name}"
    assert "sarah_c" in p.parts
    assert "digests" in p.parts
    print(f"✓ safe_data_path basic: {p.relative_to(REPO_ROOT)}")


def test_safe_data_path_rejects_bad_csm_id():
    try:
        safe_data_path("product/data/digests", "..", "leak.json")
        raise AssertionError("safe_data_path accepted csm_id='..' — containment alone is not enough")
    except HTTPException as e:
        assert e.status_code == 400
    print("✓ safe_data_path rejects csm_id='..'")


def test_safe_data_path_containment_catches_suffix_escape():
    """If suffix_parts contains '..', the containment check must catch it
    (csm_id validation alone wouldn't, since suffix_parts aren't validated).
    """
    try:
        safe_data_path("product/data/digests", "sarah_c", "..", "..", "..", "etc", "passwd")
        raise AssertionError("safe_data_path allowed escape via suffix_parts — containment check broken")
    except HTTPException as e:
        assert e.status_code == 400
        assert "Invalid path" in str(e.detail) or "Invalid" in str(e.detail)
    print("✓ safe_data_path containment catches escape via suffix_parts")


def test_safe_data_file_basic():
    p = safe_data_file("product/data/accounts", "sarah_c")
    assert p.name == "sarah_c.json", f"unexpected: {p.name}"
    print(f"✓ safe_data_file basic: {p.relative_to(REPO_ROOT)}")


def test_safe_data_file_rejects_bad_csm_id():
    for bad in ["..", ".", "foo/bar", "foo\x00bar", ".hidden"]:
        try:
            safe_data_file("product/data/accounts", bad)
            raise AssertionError(f"safe_data_file accepted bad csm_id {bad!r}")
        except HTTPException as e:
            assert e.status_code == 400
    print("✓ safe_data_file rejects bad csm_ids")


def test_safe_data_path_does_not_create_files():
    """Sanity: validation must not have side effects on the filesystem."""
    p = safe_data_path("/tmp/_csmid_test_nonexistent", "sarah_c", "x.json")
    assert not p.exists(), "safe_data_path created file as side effect"
    assert not p.parent.exists(), "safe_data_path created directory as side effect"
    print("✓ safe_data_path is side-effect-free")


def main():
    tests = [
        test_rejects_bad_inputs,
        test_accepts_good_inputs,
        test_safe_data_path_basic_construction,
        test_safe_data_path_rejects_bad_csm_id,
        test_safe_data_path_containment_catches_suffix_escape,
        test_safe_data_file_basic,
        test_safe_data_file_rejects_bad_csm_id,
        test_safe_data_path_does_not_create_files,
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
