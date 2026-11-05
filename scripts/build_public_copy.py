#!/usr/bin/env python3
"""
scripts/build_public_copy.py

Build the public copy of this repo from git, never from the working folder.

    python3 scripts/build_public_copy.py OUTPUT_DIR [--ref REF]

Steps:
  1. git archive REF (default HEAD) and extract it into OUTPUT_DIR. Only
     tracked files at REF can reach the copy; untracked and git-ignored files
     (.env, tokens/, gmail_*.json, notes/, docs/sessions/, ...) cannot.
  2. Drop paths matching scripts/public_copy_exclude.txt (read from REF).
  3. Swap in neutral pages, each read from REF (see PAGE_SWAPS): the landing
     page for index.html and placeholder privacy/terms pages for the live
     legal pages. The live files in this repo are not touched.
  4. Verify: scan EVERY file in the copy for private values using the
     SHA-256 markers in scripts/public_copy_markers.json (read from REF; the
     file itself is excluded from the copy). The only allowance is the
     company name on LICENSE's "Required Notice:" line. Reports file:line
     and kind, never the value.
  5. Run gitleaks on the copy. If gitleaks is not installed, print how to
     install it and exit non-zero: the copy is not verified clean.

Refuses to run if OUTPUT_DIR is inside the repo or is not empty.

Exit codes: 0 clean; 1 verify findings or gitleaks findings; 2 usage or
precondition error; 3 gitleaks not installed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_FILE = "scripts/public_copy_exclude.txt"
MARKERS_FILE = "scripts/public_copy_markers.json"
LANDING_SRC = "public/landing_index.html"
LANDING_DST = "index.html"

# Copy-only page swaps: {source at REF -> destination in the copy}.
# product/static/privacy.html and terms.html are the only stored copies of
# the legal pages; main.py serves them at /privacy and /terms and through
# the /static mount (/static/privacy.html, /static/terms.html). Nothing else
# (frontend build, other static folders) copies them.
PAGE_SWAPS = {
    LANDING_SRC: LANDING_DST,
    "public/privacy_placeholder.html": "product/static/privacy.html",
    "public/terms_placeholder.html": "product/static/terms.html",
}

# The single allowance in the marker scan: LICENSE's notice line names the
# licensor on purpose. Every other line of every file is scanned.
LICENSE_FILE = "LICENSE"
LICENSE_NOTICE_PREFIX = "Required Notice:"


class BuildError(Exception):
    pass


# ── git helpers ──────────────────────────────────────────────────────────────

def _git(*args: str, binary: bool = False):
    proc = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout if binary else proc.stdout.decode()


def read_from_ref(ref: str, path: str) -> str:
    try:
        return _git("show", f"{ref}:{path}")
    except BuildError as exc:
        raise BuildError(f"{path} not found at {ref}; commit it first ({exc})") from None


# ── exclusion patterns ───────────────────────────────────────────────────────

def parse_patterns(text: str) -> list[tuple[bool, str]]:
    """Return [(negated, pattern)] in file order."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        out.append((neg, line[1:] if neg else line))
    return out


def _match(pattern: str, path: str) -> bool:
    if pattern.endswith("/**"):
        base = pattern[:-3]
        return path == base or path.startswith(base + "/")
    if "/" not in pattern:
        # bare names match at the repo root only (explicit, predictable)
        return "/" not in path and fnmatch.fnmatchcase(path, pattern)
    return fnmatch.fnmatchcase(path, pattern)


def is_excluded(path: str, patterns: list[tuple[bool, str]]) -> bool:
    excluded = False
    for neg, pat in patterns:
        if _match(pat, path):
            excluded = not neg
    return excluded


# ── build ────────────────────────────────────────────────────────────────────

def check_output_dir(out: Path) -> Path:
    out = out.expanduser().resolve()
    repo = REPO_ROOT.resolve()
    if out == repo or repo in out.parents:
        raise BuildError(f"output folder must be outside the repo: {out}")
    if out in repo.parents:
        raise BuildError(f"output folder must not contain the repo: {out}")
    if out.exists():
        if not out.is_dir():
            raise BuildError(f"output path exists and is not a folder: {out}")
        if any(out.iterdir()):
            raise BuildError(f"output folder is not empty: {out}")
    return out


def build(out_dir: str | Path, ref: str = "HEAD") -> dict:
    """Extract REF into out_dir with exclusions and the neutral landing page.
    Returns a summary dict. Raises BuildError on any precondition failure."""
    out = check_output_dir(Path(out_dir))
    commit = _git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    patterns = parse_patterns(read_from_ref(commit, EXCLUDE_FILE))
    swaps = {dst: read_from_ref(commit, src) for src, dst in PAGE_SWAPS.items()}

    archive = _git("archive", "--format=tar", commit, binary=True)
    out.mkdir(parents=True, exist_ok=True)
    kept, dropped = [], []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar.getmembers():
            name = member.name
            if member.isdir():
                continue
            if not (member.isfile() or member.issym()):
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                raise BuildError(f"unsafe path in archive: {name}")
            if is_excluded(name, patterns):
                dropped.append(name)
                continue
            if member.issym():
                # Symlinks could point outside the copy; refuse rather than guess.
                raise BuildError(f"symlink in archive not supported: {name}")
            dest = out / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, open(dest, "wb") as fh:
                shutil.copyfileobj(src, fh)
            dest.chmod(member.mode & 0o777 or 0o644)
            kept.append(name)

    for dst, text in swaps.items():
        target = out / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if dst not in kept:
            kept.append(dst)

    return {
        "commit": commit, "output": str(out), "kept": sorted(kept),
        "dropped": sorted(dropped), "swapped": dict(PAGE_SWAPS),
    }


# ── verify: private markers ──────────────────────────────────────────────────

def load_markers(ref: str = "HEAD") -> dict:
    data = json.loads(read_from_ref(ref, MARKERS_FILE))
    return {
        "substrings": set(data["substrings"]),
        "substring_lengths": sorted(set(data["substring_lengths"])),
        "tokens": set(data["tokens"]),
        "clerk_ids": set(data["clerk_ids"]),
        "email_domains": set(data["email_domains"]),
        "domains_anywhere": set(data["domains_anywhere"]),
        "company_name": set(data.get("company_name", [])),
        "company_name_lengths": sorted(set(data.get("company_name_lengths", []))),
        "allowed_company_lines": {
            path: set(hashes) for path, hashes in data.get("allowed_company_lines", {}).items()
        },
    }


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


_ALPHA_RUN = re.compile(r"[a-z]+")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,}")
_CLERK = re.compile(r"user_[A-Za-z0-9]{27}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
_DOMAIN = re.compile(r"(?<![A-Za-z0-9-])((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")


def _has_substring(word: str, hashes: set, lengths: list) -> bool:
    for L in lengths:
        for i in range(len(word) - L + 1):
            if _h(word[i:i + L]) in hashes:
                return True
    return False


def line_hits(line: str, m: dict, allow_company: bool = False) -> list[str]:
    """Kinds of private value found on one line (never the value)."""
    hits: list[str] = []
    low = line.lower()
    for run in _ALPHA_RUN.finditer(low):
        word = run.group(0)
        if _has_substring(word, m["substrings"], m["substring_lengths"]):
            hits.append("personal name or domain")
        if not allow_company and _has_substring(
            word, m.get("company_name", set()), m.get("company_name_lengths", [])
        ):
            hits.append("company name")
    for t in _TOKEN.finditer(line):
        if _h(t.group(0)) in m["tokens"]:
            hits.append("private identifier (form or project id)")
    for c in _CLERK.finditer(line):
        if _h(c.group(0)) in m["clerk_ids"]:
            hits.append("production Clerk id")
    for e in _EMAIL.finditer(line):
        if _h(e.group(1).lower()) in m["email_domains"]:
            hits.append("operator contact address")
    for d in _DOMAIN.finditer(line):
        parts = d.group(1).lower().split(".")
        if any(_h(".".join(parts[i:])) in m["domains_anywhere"] for i in range(len(parts) - 1)):
            hits.append("personal domain")
    return hits


def verify_markers(out_dir: str | Path, markers: dict) -> list[str]:
    """Return 'path:line: kind' findings for every file in the copy at
    out_dir. No file is skipped; binary files are scanned as decoded bytes.
    The company name is allowed only on LICENSE's Required Notice line and on
    lines whose exact content hash is listed in allowed_company_lines (the
    two README credit lines). Every other check applies to every line."""
    out = Path(out_dir)
    findings: list[str] = []
    for p in sorted(out.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(out).as_posix()
        text = p.read_bytes().decode("utf-8", errors="ignore")
        allowed_hashes = markers.get("allowed_company_lines", {}).get(rel, set())
        for n, line in enumerate(text.splitlines(), 1):
            allow = (
                (rel == LICENSE_FILE and line.startswith(LICENSE_NOTICE_PREFIX))
                or _h(line) in allowed_hashes
            )
            for kind in dict.fromkeys(line_hits(line, markers, allow_company=allow)):
                findings.append(f"{rel}:{n}: {kind}")
    return findings


# ── verify: gitleaks ─────────────────────────────────────────────────────────

GITLEAKS_INSTALL = (
    "gitleaks is not installed, so the copy is NOT verified clean.\n"
    "Install it, then re-run this script:\n"
    "  macOS:  brew install gitleaks\n"
    "  other:  https://github.com/gitleaks/gitleaks#installing"
)


def run_gitleaks(out_dir: str | Path) -> tuple[int, list[str]]:
    """Return (status, findings). status: 0 clean, 1 findings, 3 missing.
    Findings are 'path:line: rule' only; gitleaks runs with --redact."""
    exe = shutil.which("gitleaks")
    if not exe:
        return 3, []
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "report.json"
        base = ["--no-banner", "--redact", "--report-format", "json",
                "--report-path", str(report), "--exit-code", "1"]
        proc = subprocess.run([exe, "dir", str(out_dir), *base], capture_output=True, text=True)
        if proc.returncode not in (0, 1):  # older gitleaks without "dir"
            proc = subprocess.run([exe, "detect", "--no-git", "--source", str(out_dir), *base],
                                  capture_output=True, text=True)
        if proc.returncode not in (0, 1):
            raise BuildError(f"gitleaks failed to run (exit {proc.returncode})")
        items = json.loads(report.read_text() or "[]") if report.exists() else []
    out = Path(out_dir).resolve()
    findings = []
    for it in items:
        f = Path(it.get("File", ""))
        try:
            f = f.resolve().relative_to(out)
        except ValueError:
            pass
        findings.append(f"{f.as_posix()}:{it.get('StartLine', '?')}: {it.get('RuleID', 'secret')}")
    return (1 if findings else 0), sorted(set(findings))


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the public copy of this repo from git.")
    ap.add_argument("output", help="empty folder outside the repo (created if missing)")
    ap.add_argument("--ref", default="HEAD", help="git ref to build from (default HEAD)")
    args = ap.parse_args(argv)

    try:
        summary = build(args.output, args.ref)
        markers = load_markers(summary["commit"])
    except BuildError as exc:
        print(f"[build_public_copy] ERROR: {exc}", file=sys.stderr)
        return 2

    out = Path(summary["output"])
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"[build_public_copy] built {summary['commit'][:12]} -> {out}")
    print(f"[build_public_copy] {len(summary['kept'])} files, {size:,} bytes; "
          f"{len(summary['dropped'])} tracked files excluded")

    status = 0
    findings = verify_markers(out, markers)
    if findings:
        print(f"[verify] FAIL: {len(findings)} private value(s) found (values not shown):")
        for f in findings:
            print(f"  {f}")
        status = 1
    else:
        print("[verify] OK: no private markers found")

    gl_status, gl_findings = run_gitleaks(out)
    if gl_status == 3:
        print(f"[gitleaks] {GITLEAKS_INSTALL}")
        return 3 if status == 0 else status
    if gl_findings:
        print(f"[gitleaks] FAIL: {len(gl_findings)} finding(s) (redacted):")
        for f in gl_findings:
            print(f"  {f}")
        status = 1
    else:
        print("[gitleaks] OK: no leaks found")

    print("[build_public_copy] CLEAN" if status == 0 else "[build_public_copy] NOT CLEAN")
    return status


if __name__ == "__main__":
    sys.exit(main())
