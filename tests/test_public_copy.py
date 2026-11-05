"""
tests/test_public_copy.py

scripts/build_public_copy.py builds the public copy from git (git archive of
HEAD), applies scripts/public_copy_exclude.txt, swaps in the neutral landing
page and scans for private markers.

Checks, on a copy built into a temp folder:
  - excluded paths are absent; docs/policies/ is kept
  - the example configs are present
  - index.html is the neutral landing page
  - no git-ignored file reaches the copy, even when such files exist in the
    working folder (probe files are created for the test and removed after)
  - every copied file is tracked at HEAD (nothing read from the folder)
  - the copy has no private markers
  - the script refuses an output folder inside the repo or one with files

The build reads its inputs from HEAD, so the script, exclude list, landing
page and markers must be committed.

Standalone-runnable:
    python3 tests/test_public_copy.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

_spec = importlib.util.spec_from_file_location(
    "build_public_copy", REPO_ROOT / "scripts" / "build_public_copy.py"
)
bpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpc)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, check=True).stdout


def _tracked_at_head() -> set[str]:
    return set(_git("ls-tree", "-r", "--name-only", "HEAD").split("\n")) - {""}


def _head_has_tooling() -> bool:
    tracked = _tracked_at_head()
    return all(p in tracked for p in (bpc.EXCLUDE_FILE, bpc.LANDING_SRC, bpc.MARKERS_FILE))


pytestmark = pytest.mark.skipif(
    not _head_has_tooling(), reason="build inputs not committed at HEAD yet"
)

# Marker written into probe files. Built at runtime so this test file (which
# ships in the copy) does not itself contain it.
_PROBE_TEXT = "-".join(["PUBLIC", "COPY", "PROBE"])

# Git-ignored probe files placed in the working folder during the build.
_PROBES = [
    ".env",
    ".env.publiccopy-probe",
    "tokens/publiccopy-probe.json",
    "gmail_credentials.json",
    "gmail_token.json",
    "notes/publiccopy-probe.md",
    "product/data/publiccopy-probe.json",
    "docs/sessions/publiccopy-probe.md",
    "docs/handoffs/publiccopy-probe.md",
    ".claude/publiccopy-probe.json",
]


@pytest.fixture(scope="module")
def copy_dir(tmp_path_factory):
    created: list[Path] = []
    made_dirs: list[Path] = []
    for rel in _PROBES:
        p = REPO_ROOT / rel
        assert subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO_ROOT).returncode == 0, \
            f"probe {rel} must be git-ignored"
        if p.exists():
            continue  # a real file is already there; never overwrite it
        for parent in reversed(p.relative_to(REPO_ROOT).parents[:-1]):
            d = REPO_ROOT / parent
            if not d.exists():
                d.mkdir()
                made_dirs.append(d)
        p.write_text(f"{_PROBE_TEXT} secret=do-not-ship\n")
        created.append(p)
    out = tmp_path_factory.mktemp("public_copy") / "copy"
    try:
        summary = bpc.build(out)
    finally:
        for p in created:
            p.unlink()
        for d in reversed(made_dirs):
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
    return out, summary


EXCLUDED = [
    "CLAUDE.md",
    "CODE_REVIEW.html",
    "CS_Agent_Demo_Script.docx",
    "PRODUCT_BUILD_SPEC.md",
    "PILOT_PRODUCT_BUILD_SPEC.md",
    "org_config.json",
    "org_config.json.bak",
    "pilot_config.json",
    "public/landing_index.html",
    "scripts/public_copy_markers.json",
]


def test_excluded_paths_absent(copy_dir):
    out, _ = copy_dir
    for rel in EXCLUDED:
        assert not (out / rel).exists(), f"{rel} must not be in the copy"


def test_docs_dropped_except_policies(copy_dir):
    out, _ = copy_dir
    docs = sorted(p.relative_to(out).as_posix() for p in (out / "docs").rglob("*") if p.is_file())
    assert docs, "docs/policies should be kept"
    assert all(d.startswith("docs/policies/") for d in docs), docs
    for name in ("incident-response-policy.md", "information-security-policy.md",
                 "vulnerability-disclosure-policy.md"):
        assert (out / "docs" / "policies" / name).is_file()


def test_example_configs_and_core_files_present(copy_dir):
    out, _ = copy_dir
    for rel in ("org_config.example.json", "pilot_config.example.json", "LICENSE",
                "README.md", "main.py", ".env.example", "scripts/build_public_copy.py",
                "scripts/public_copy_exclude.txt"):
        assert (out / rel).is_file(), rel


def test_index_is_neutral_landing_page(copy_dir):
    out, _ = copy_dir
    html = (out / "index.html").read_text(encoding="utf-8")
    assert html == _git("show", f"HEAD:{bpc.LANDING_SRC}")
    assert html != _git("show", "HEAD:index.html")
    low = html.lower()
    # The company name is built at runtime so this file (which ships in the
    # copy) does not contain it.
    company = "al" + "waison"
    for banned in ("docs.google.com/forms", "{{waitlist_form_url}}", "cdn-cgi",
                   "cloudflare", company, "<script", "googletagmanager", "analytics"):
        assert banned not in low, banned
    assert "readme" in low


def test_live_index_untouched_in_repo():
    assert (REPO_ROOT / "index.html").read_text(encoding="utf-8") == _git("show", "HEAD:index.html")


def test_no_ignored_files_in_copy(copy_dir):
    out, _ = copy_dir
    files = [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()]
    for rel in _PROBES:
        assert rel not in files, rel
    bad = [
        f for f in files
        if f.startswith((".env", "notes/", "product/data/", ".claude/", "docs/sessions/", "docs/handoffs/"))
        and f != ".env.example"
        or (f.startswith("tokens/") and f != "tokens/.gitkeep")
        or (f.startswith("gmail_") and f.endswith(".json"))
    ]
    assert not bad, bad
    for f in files:
        assert _PROBE_TEXT not in (out / f).read_text(errors="ignore"), f


def test_every_file_is_tracked_at_head(copy_dir):
    out, summary = copy_dir
    tracked = _tracked_at_head()
    files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert files - {"index.html"} <= tracked
    assert set(summary["kept"]) == files


def test_copy_has_no_private_markers(copy_dir):
    out, _ = copy_dir
    findings = bpc.verify_markers(out, bpc.load_markers("HEAD"))
    assert findings == [], "\n".join(findings)


def test_refuses_output_inside_repo():
    with pytest.raises(bpc.BuildError, match="outside the repo"):
        bpc.build(REPO_ROOT / "tmp_public_copy_should_not_exist")
    with pytest.raises(bpc.BuildError, match="outside the repo"):
        bpc.build(REPO_ROOT)
    assert not (REPO_ROOT / "tmp_public_copy_should_not_exist").exists()


def test_refuses_non_empty_output(tmp_path):
    (tmp_path / "existing.txt").write_text("x")
    with pytest.raises(bpc.BuildError, match="not empty"):
        bpc.build(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing.txt"]


def test_exclude_pattern_semantics():
    pats = bpc.parse_patterns("# c\n\nCLAUDE.md\ndocs/**\n!docs/policies/**\n*.bak\n")
    assert bpc.is_excluded("CLAUDE.md", pats)
    assert not bpc.is_excluded("sub/CLAUDE.md", pats)
    assert bpc.is_excluded("docs/plans/x.md", pats)
    assert not bpc.is_excluded("docs/policies/a.md", pats)
    assert bpc.is_excluded("org_config.json.bak", pats)
    assert not bpc.is_excluded("main.py", pats)


def test_verify_reports_location_not_value(tmp_path):
    """The scan reports path:line and a kind, never the matched text."""
    probe_id = "user_" + "Q" * 27
    markers = {
        "substrings": {bpc._h("zzqx")}, "substring_lengths": [4],
        "tokens": {bpc._h("tok-ABCDEFGH-123")}, "clerk_ids": {bpc._h(probe_id)},
        "email_domains": {bpc._h("contact.test")}, "domains_anywhere": {bpc._h("blocked.test")},
    }
    (tmp_path / "a.txt").write_text(
        f"ok line\nname xxzzqxyy\nid {probe_id}\nmail hi@contact.test\n"
        "see blocked.test\nform tok-ABCDEFGH-123\n"
    )
    (tmp_path / "LICENSE").write_text("zzqx not exempt\n")
    (tmp_path / "README.md").write_text("zzqx here\n## License\nzzqx not exempt either\n")
    found = bpc.verify_markers(tmp_path, markers)
    assert found == [
        "LICENSE:1: personal name or domain",
        "README.md:1: personal name or domain",
        "README.md:3: personal name or domain",
        "a.txt:2: personal name or domain",
        "a.txt:3: production Clerk id",
        "a.txt:4: operator contact address",
        "a.txt:5: personal domain",
        "a.txt:6: private identifier (form or project id)",
    ]
    for line in found:
        for value in ("zzqx", probe_id, "contact.test", "blocked.test", "tok-ABCDEFGH"):
            assert value not in line


# ── Placeholder legal pages and the strict company-name scan ────────────────

import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402

_COMPANY = "al" + "waison"  # built at runtime: this file ships in the copy

# Git blob ids of the live, CASA-locked legal pages. They must never change.
LIVE_LEGAL_BLOBS = {
    "product/static/privacy.html": "fe726a2e7f75e3d3f142bba72077190291744b0b",
    "product/static/terms.html": "85b7069f6d2bd6c7cd579cab720e56f75799056e",
}

_private_repo = (REPO_ROOT / "scripts" / "public_copy_markers.json").exists()


@pytest.mark.parametrize("dst,src", [
    ("product/static/privacy.html", "public/privacy_placeholder.html"),
    ("product/static/terms.html", "public/terms_placeholder.html"),
])
def test_copy_legal_pages_are_placeholders(copy_dir, dst, src):
    out, summary = copy_dir
    html = (out / dst).read_text(encoding="utf-8")
    assert html == _git("show", f"HEAD:{src}")
    assert summary["swapped"][src] == dst
    low = html.lower()
    assert "@" not in html, "placeholder must not contain an email address"
    assert _COMPANY not in low
    assert "inc." not in low and "llc" not in low
    assert "<script" not in low
    assert "replace" in low and "before going live" in low
    assert html != _git("show", f"HEAD:{dst}")


@pytest.mark.skipif(not _private_repo, reason="live legal pages exist only in the private repo")
def test_live_legal_pages_unchanged_at_head():
    for path, blob in LIVE_LEGAL_BLOBS.items():
        assert _git("rev-parse", f"HEAD:{path}").strip() == blob, f"{path} changed at HEAD"
        working = subprocess.run(["git", "hash-object", path], cwd=REPO_ROOT,
                                 capture_output=True, text=True, check=True).stdout.strip()
        assert working == blob, f"{path} changed in the working tree"


def _strict_copy(copy_dir, tmp_path) -> Path:
    out, _ = copy_dir
    dst = tmp_path / "copy"
    shutil.copytree(out, dst)
    return dst


@pytest.mark.parametrize("page", ["product/static/privacy.html", "product/static/terms.html"])
def test_scan_fails_on_planted_company_address_in_policy_page(copy_dir, tmp_path, page):
    copy = _strict_copy(copy_dir, tmp_path)
    target = copy / page
    lines = target.read_text(encoding="utf-8").splitlines()
    lines.insert(5, f'<p>Contact: <a href="mailto:hello@{_COMPANY}.com">us</a></p>')
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    found = bpc.verify_markers(copy, bpc.load_markers("HEAD"))
    assert f"{page}:6: company name" in found
    assert f"{page}:6: operator contact address" in found
    assert all(f.startswith(f"{page}:6:") for f in found), found


def test_scan_fails_on_company_name_anywhere_except_allowed_lines(copy_dir, tmp_path):
    copy = _strict_copy(copy_dir, tmp_path)
    markers = bpc.load_markers("HEAD")
    assert bpc.verify_markers(copy, markers) == []  # allowed README lines + LICENSE notice
    readme = copy / "README.md"
    n = len(readme.read_text(encoding="utf-8").splitlines())
    with readme.open("a", encoding="utf-8") as fh:
        fh.write(f"Another mention of {_COMPANY.title()} here.\n")
    with (copy / "LICENSE").open("a", encoding="utf-8") as fh:
        fh.write(f"Copyright {_COMPANY.title()}\n")
    (copy / "product" / "static" / "docs.html").write_text(f"<p>{_COMPANY.upper()}</p>\n")
    found = bpc.verify_markers(copy, markers)
    license_n = len((copy / "LICENSE").read_text(encoding="utf-8").splitlines())
    assert found == sorted([
        f"LICENSE:{license_n}: company name",
        f"README.md:{n + 1}: company name",
        "product/static/docs.html:1: company name",
    ]), found


def test_readme_allowance_is_exact_line_content():
    markers = json.loads((REPO_ROOT / "scripts" / "public_copy_markers.json").read_text()) \
        if _private_repo else None
    if markers is None:
        pytest.skip("markers file not present")
    allowed = set(markers["allowed_company_lines"]["README.md"])
    assert list(markers["allowed_company_lines"]) == ["README.md"]
    lines = (REPO_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    hits = [i for i, l in enumerate(lines, 1) if hashlib.sha256(l.encode()).hexdigest() in allowed]
    assert hits == [3, 6]
    mentions = [i for i, l in enumerate(lines, 1) if _COMPANY in l.lower()]
    assert mentions == [3, 6]
    # A trailing space is a different line: not allowed.
    assert hashlib.sha256((lines[2] + " ").encode()).hexdigest() not in allowed


def test_build_reads_swapped_pages_from_ref_not_folder(tmp_path, monkeypatch):
    """Editing the working copy of a placeholder must not change the copy."""
    src = REPO_ROOT / "public" / "privacy_placeholder.html"
    original = src.read_bytes()
    try:
        src.write_text("WORKING-FOLDER-EDIT\n")
        out = tmp_path / "c"
        bpc.build(out)
        assert (out / "product/static/privacy.html").read_text() == _git(
            "show", "HEAD:public/privacy_placeholder.html")
    finally:
        src.write_bytes(original)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
