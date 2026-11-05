"""
product/csm_id_validation.py

Single source of truth for csm_id sanitization and safe filesystem paths
constructed from csm_id values. Used everywhere csm_id reaches disk —
request handlers, scheduler jobs, OAuth callbacks, connector token storage.

Pre-fix (Day 2 audit), the validator in main.py accepted '..' because the
regex [a-zA-Z0-9_.-]+ does not exclude '..' as a substring. Path traversal
was live in admin/trigger-digest, admin/google-reauth, and the chat
handler's _chat_csm_id usage.

Validation is fail-closed: any unexpected shape raises HTTPException(400).
The path helpers add a containment check (resolve() + relative_to()) as
belt-and-suspenders against future regex drift.
"""

import re
from pathlib import Path

from fastapi import HTTPException

_CSM_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")
_MAX_LEN = 64  # Clerk user_id is 'user_' + 27 base62 = 32; legacy ids like 'sarah.c' are short


def validate_csm_id(csm_id: str) -> str:
    """Validate csm_id, return it unchanged on success.

    Raises HTTPException(400) on any of:
    - empty string or None
    - longer than 64 chars
    - contains characters outside [a-zA-Z0-9_.-]
    - contains '..' anywhere (path traversal)
    - is bare '.' or '..'
    - starts or ends with '.' (hidden files / trailing-dot path tricks)
    - contains a null byte (defense in depth; regex would also reject)
    """
    if not isinstance(csm_id, str) or not csm_id:
        raise HTTPException(status_code=400, detail="Invalid csm_id")
    if len(csm_id) > _MAX_LEN:
        raise HTTPException(status_code=400, detail="Invalid csm_id")
    if "\x00" in csm_id:
        raise HTTPException(status_code=400, detail="Invalid csm_id")
    if not _CSM_ID_RE.match(csm_id):
        raise HTTPException(status_code=400, detail="Invalid csm_id")
    if ".." in csm_id or csm_id in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid csm_id")
    if csm_id.startswith(".") or csm_id.endswith("."):
        raise HTTPException(status_code=400, detail="Invalid csm_id")
    return csm_id


def safe_data_path(base_dir: str | Path, csm_id: str, *suffix_parts: str) -> Path:
    """Build base_dir/csm_id[/*suffix_parts], validated and contained.

    Used when csm_id is a directory level under base_dir, e.g.:
        safe_data_path("product/data/digests", csm_id, "2026-04-18.json")
        → product/data/digests/<csm_id>/2026-04-18.json

    Validates csm_id, resolves the full path, and asserts the resolved
    target is contained within the resolved base_dir. Raises
    HTTPException(400) on any failure.

    suffix_parts are NOT validated as csm_id (they're typically dates or
    fixed filenames built by the caller). They MUST NOT come from
    untrusted input — caller's responsibility.
    """
    csm_id = validate_csm_id(csm_id)
    base = Path(base_dir).resolve()
    target = base / csm_id
    if suffix_parts:
        target = target.joinpath(*suffix_parts)
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    return target_resolved


def safe_data_file(base_dir: str | Path, csm_id: str, suffix: str = ".json") -> Path:
    """Build base_dir/{csm_id}{suffix}, validated and contained.

    Used when csm_id is the filename stem under base_dir, e.g.:
        safe_data_file("product/data/accounts", csm_id)
        → product/data/accounts/<csm_id>.json

    Same fail-closed semantics as safe_data_path. The suffix is appended
    literally; caller's responsibility to keep it from untrusted input.
    """
    csm_id = validate_csm_id(csm_id)
    base = Path(base_dir).resolve()
    target = (base / f"{csm_id}{suffix}").resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    return target
