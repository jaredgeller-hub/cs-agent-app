"""
scripts/backfill_token_encryption.py

Re-encrypts every legacy plaintext OAuth token row in the connections
table via product.token_store.save_token (which performs the encryption).
Already-encrypted rows are skipped (idempotent, resumable).

Usage:
    python3 scripts/backfill_token_encryption.py [--dry-run]

Environment:
    DATABASE_URL          — Postgres connection (Render injects this)
    TOKEN_ENCRYPTION_KEY  — Fernet key used by product.token_store

Behavior:
    --dry-run: enumerate rows and report what *would* be re-encrypted.
               No writes, no encryption attempts. Use this for a sanity
               check before the live run.

    live run : iterate ALL connection rows (any status); for each legacy
               row (token_data shape is NOT {"ciphertext": "gAAAAA..."}),
               call save_token which re-encrypts and upserts atomically.
               Re-running is safe — already-encrypted rows are detected
               by shape and skipped.

Output: one line per row plus a summary block. Suitable for piping
to a file for the audit trail.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _is_encrypted_shape(raw) -> bool:
    """Mirrors product.token_store._is_encrypted_shape — local copy so the
    script doesn't depend on import-time side-effects beyond DB + crypto."""
    if not isinstance(raw, dict):
        return False
    if set(raw.keys()) != {"ciphertext"}:
        return False
    value = raw.get("ciphertext")
    return isinstance(value, str) and value.startswith("gAAAAA")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Enumerate rows and report what would be re-encrypted; no writes.",
    )
    args = parser.parse_args()

    # Import here so a --help invocation works without DB/env wiring.
    # Import the module (not the names) so init_db()'s reassignment of the
    # SessionLocal module global is visible to us — `from X import Y` would
    # bind the local name to the original None default.
    import product.database as _db
    from product.database import init_db, Connection
    from product.token_store import save_token

    if not init_db():
        print("ERROR: init_db() returned False — DATABASE_URL not set?",
              file=sys.stderr)
        return 1

    session = _db.SessionLocal()
    try:
        # Scan ALL rows regardless of status. The earlier status="active"
        # filter left plaintext rows in non-active states (expired / revoked /
        # missing) structurally unreachable, so they were never re-encrypted.
        # The shape check below makes this safe to run over the full table:
        # already-encrypted rows are skipped, status is irrelevant.
        rows = session.query(Connection).all()
    finally:
        session.close()

    total = len(rows)
    already_encrypted = 0
    encrypted_now = 0
    skipped_empty = 0
    failed = 0

    print(f"[backfill] {total} connection rows found (all statuses) "
          f"(dry_run={args.dry_run})")

    for row in rows:
        cid = row.customer_id
        provider = row.provider
        raw = dict(row.token_data) if isinstance(row.token_data, dict) else None

        if raw is None:
            skipped_empty += 1
            print(f"  skip:    customer_id={cid} provider={provider} "
                  f"reason=empty_token_data")
            continue

        if _is_encrypted_shape(raw):
            already_encrypted += 1
            print(f"  already: customer_id={cid} provider={provider}")
            continue

        if args.dry_run:
            print(f"  would:   customer_id={cid} provider={provider}")
            continue

        try:
            save_token(cid, provider, raw)
            encrypted_now += 1
            print(f"  ok:      customer_id={cid} provider={provider}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL:    customer_id={cid} provider={provider} "
                  f"exc_type={type(exc).__name__}",
                  file=sys.stderr)

    print()
    print("[backfill] summary:")
    print(f"  total            = {total}")
    print(f"  already_encrypted= {already_encrypted}")
    print(f"  encrypted_now    = {encrypted_now}")
    print(f"  skipped_empty    = {skipped_empty}")
    print(f"  failed           = {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
