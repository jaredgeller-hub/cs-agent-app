"""
tests/test_gitignore_exceptions.py

.gitignore must let the two committed placeholders through a normal
`git add` while still ignoring every real secret:

  - picked up:  .env.example, frontend/.env.example, tokens/.gitkeep
  - ignored:    .env, .env.*, frontend/.env, frontend/.env.*, every file
                under tokens/ other than .gitkeep (including nested ones)

Checked in two places:
  1. this repo, with `git check-ignore --no-index` (tracked state ignored,
     so the rules themselves are tested)
  2. a public copy built by scripts/build_public_copy.py into a temp folder:
     `git init` + `git add -A` (no force-add) must stage every file in the
     copy, including .env.example and tokens/.gitkeep, and must not stage
     secret files planted in the copy after the build

Standalone-runnable:
    python3 tests/test_gitignore_exceptions.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "build_public_copy", REPO_ROOT / "scripts" / "build_public_copy.py"
)
bpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpc)

MUST_BE_ADDABLE = [
    ".env.example",
    "frontend/.env.example",
    "tokens/.gitkeep",
]

MUST_BE_IGNORED = [
    ".env",
    ".env.local",
    ".env.production",
    ".env.save",
    "frontend/.env",
    "frontend/.env.local",
    "frontend/.env.production",
    "tokens/sarah_c.json",
    "tokens/user_abc_hubspot.json",
    "tokens/sarah_c.json.stale_backup_20260506",
    "tokens/nested/deeper.json",
    "tokens/.hidden.json",
    "gmail_credentials.json",
    "gmail_token.json",
]


def _is_ignored(rel: str, cwd: Path = REPO_ROOT) -> bool:
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", rel], cwd=cwd, check=False
    )
    assert proc.returncode in (0, 1), f"git check-ignore failed for {rel}"
    return proc.returncode == 0


# ── 1. rules in this repo ────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", MUST_BE_ADDABLE)
def test_placeholders_are_not_ignored(rel):
    assert not _is_ignored(rel), f"{rel} must be picked up by a normal git add"


@pytest.mark.parametrize("rel", MUST_BE_IGNORED)
def test_secrets_are_ignored(rel):
    assert _is_ignored(rel), f"{rel} must stay git-ignored"


def test_every_real_token_file_in_working_folder_is_ignored():
    tokens = REPO_ROOT / "tokens"
    if not tokens.is_dir():
        pytest.skip("no tokens/ folder in this checkout")
    for p in tokens.rglob("*"):
        if p.is_file() and p.name != ".gitkeep":
            rel = p.relative_to(REPO_ROOT).as_posix()
            assert _is_ignored(rel), f"{rel} must stay git-ignored"


def test_placeholders_are_tracked():
    tracked = set(subprocess.run(
        ["git", "ls-files", *MUST_BE_ADDABLE], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.split())
    assert tracked == set(MUST_BE_ADDABLE)


# ── 2. public copy: git init + git add -A ────────────────────────────────────

def _head_has_tooling() -> bool:
    tracked = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    return all(p in tracked for p in (bpc.EXCLUDE_FILE, bpc.LANDING_SRC, bpc.MARKERS_FILE))


@pytest.mark.skipif(not _head_has_tooling(), reason="public-copy tooling not committed at HEAD")
def test_public_copy_git_add_stages_placeholders_not_secrets(tmp_path):
    out = tmp_path / "copy"
    bpc.build(out)
    built = sorted(
        p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()
    )
    for rel in ("tokens/.gitkeep", ".env.example", ".gitignore"):
        assert rel in built, f"{rel} missing from the public copy"

    # Secrets an operator would create after cloning the copy.
    planted = [".env", ".env.local", "frontend/.env", "tokens/me.json",
               "tokens/me_hubspot.json", "tokens/nested/x.json"]
    for rel in planted:
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("secret=do-not-stage\n")

    def git(*args):
        return subprocess.run(
            ["git", "-c", "init.defaultBranch=main", *args], cwd=out,
            capture_output=True, text=True, check=True,
        ).stdout

    git("init", "-q")
    git("add", "-A")
    staged = sorted(git("ls-files", "--cached").split("\n")[:-1])

    assert staged == built, (
        f"not staged: {sorted(set(built) - set(staged))}; "
        f"unexpected: {sorted(set(staged) - set(built))}"
    )
    for rel in planted:
        assert rel not in staged, f"{rel} was staged in the public copy"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
