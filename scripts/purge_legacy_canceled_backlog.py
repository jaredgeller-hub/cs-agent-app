"""
scripts/purge_legacy_canceled_backlog.py

One-time close of the pre-pilot canceled backlog: rows canceled before Step 2
shipped, which carry canceled_at = NULL and are therefore invisible to the
recurring sweep (product.database.purge_expired_cancellations gathers only
canceled_at IS NOT NULL rows).

This script introduces NO new deletion logic. The purge is performed by
purge_expired_cancellations (the peer-reviewed, tested recurring sweep)
invoked once with max_per_run raised to the human-reviewed count. Migration
007 (007_backfill_legacy_canceled_at.sql) must be applied FIRST to backfill
canceled_at = trial_ends_at on the trial-origin set; until then the sweep
finds nothing to purge.

Two views are reported in --dry-run, both read-only:
  legacy backlog   canceled + canceled_at IS NULL (work still pending the
                   backfill). After migration 007 this should fall to the
                   hand-handle rows only (paid-origin / null trial_ends_at).
  sweep-eligible   canceled + NOT comp + canceled_at IS NOT NULL + older than
                   the 29-day cutoff. Computed with the EXACT gather predicate
                   of purge_expired_cancellations, so its count is the number
                   --confirm will purge. This is your N.

The canonical privacy-breach number is NOT computed here (to avoid drifting
from the real metric). Read it via POST /admin/audit-snapshot with the
X-Admin-Key header, key canceled_users_in_privacy_breach, before and after.

Usage:
    # Look (read-only). Run before AND after applying migration 007.
    python3 scripts/purge_legacy_canceled_backlog.py --dry-run

    # Purge, after migration 007 and after reviewing the dry-run.
    python3 scripts/purge_legacy_canceled_backlog.py --confirm --expect N

Flags:
    --dry-run     Report both views. No writes. Default when --confirm absent.
    --confirm     Required to purge. Without it the script only reports.
    --expect N    Required with --confirm. The sweep-eligible count you saw in
                  the dry-run. Passed as max_per_run: if MORE than N rows are
                  eligible at run time, the sweep trips its cap and purges
                  NOTHING, forcing a re-review. The script also aborts before
                  calling the sweep if N does not match the freshly-gathered
                  eligible count.

Environment:
    DATABASE_URL  — Postgres connection (Render injects this).

Pipe output to a timestamped file for the audit trail.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("cs-agent.legacy-backlog-purge")

_CUTOFF_DAYS = 29  # must match purge_expired_cancellations


def _report(session) -> int:
    """Read-only. Print the legacy-backlog breakdown and the sweep-eligible
    count. Returns the sweep-eligible count (the N for --confirm)."""
    from product.database import Customer, as_utc

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=_CUTOFF_DAYS)

    legacy = session.query(Customer).filter(
        Customer.subscription_status == "canceled",
        Customer.canceled_at.is_(None),
    ).all()

    backfillable = [
        c for c in legacy
        if c.stripe_subscription_id is None
        and not c.is_comp_account
        and c.clerk_user_id != "sarah_c"
        and c.trial_ends_at is not None
    ]
    null_trial_end = [
        c for c in legacy
        if c.stripe_subscription_id is None and c.trial_ends_at is None
    ]
    paid_origin = [c for c in legacy if c.stripe_subscription_id is not None]
    comp_in_legacy = [c for c in legacy if c.is_comp_account]
    soft_deleted = [c for c in legacy if c.is_deleted]

    candidates = session.query(Customer).filter(
        Customer.subscription_status == "canceled",
        Customer.is_comp_account.is_(False),
        Customer.canceled_at.isnot(None),
    ).all()
    eligible = [
        c for c in candidates
        if c.clerk_user_id and as_utc(c.canceled_at) < cutoff
    ]

    print("=== legacy backlog (canceled, canceled_at IS NULL) ===")
    print(f"  total                        = {len(legacy)}")
    print(f"  trial-origin backfillable    = {len(backfillable)}")
    print(f"  trial-origin null trial_end  = {len(null_trial_end)}  (hand-handle)")
    print(f"  paid-origin (has stripe sub) = {len(paid_origin)}  (hand-handle)")
    print(f"  comp accounts in legacy      = {len(comp_in_legacy)}  (excluded)")
    print(f"  soft-deleted in legacy       = {len(soft_deleted)}")
    print()
    for c in legacy:
        print(f"    id={c.clerk_user_id} email={c.email} "
              f"stripe_sub={c.stripe_subscription_id} comp={c.is_comp_account} "
              f"deleted={c.is_deleted} trial_ends_at={c.trial_ends_at}")
    print()
    print(f"=== sweep-eligible now (canceled, not comp, canceled_at older than {_CUTOFF_DAYS}d) ===")
    print(f"  eligible_count (this is your --expect N) = {len(eligible)}")
    for c in eligible:
        print(f"    id={c.clerk_user_id} email={c.email} canceled_at={c.canceled_at}")
    print()
    print("Canonical breach number: POST /admin/audit-snapshot (X-Admin-Key), "
          "key canceled_users_in_privacy_breach. Read before and after.")
    return len(eligible)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; no writes. Default when --confirm absent.")
    parser.add_argument("--confirm", action="store_true",
                        help="Required to actually purge.")
    parser.add_argument("--expect", type=int, default=None,
                        help="Required with --confirm: the sweep-eligible count you reviewed.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Module import (not name import) so init_db()'s SessionLocal rebind is
    # visible here. Mirrors scripts/backfill_token_encryption.py.
    import product.database as _db
    from product.database import init_db, purge_expired_cancellations

    if not init_db():
        print("ERROR: init_db() returned False — DATABASE_URL not set?", file=sys.stderr)
        return 1

    session = _db.SessionLocal()
    try:
        eligible_n = _report(session)
    finally:
        session.close()

    if not args.confirm:
        print("\n[dry-run] No changes made. Re-run with --confirm --expect N to purge.")
        return 0

    if args.expect is None:
        print("\nERROR: --confirm requires --expect N (the count you reviewed).",
              file=sys.stderr)
        return 1

    print(f"\n[purge] --confirm with --expect {args.expect}. "
          f"Freshly gathered eligible = {eligible_n}.")
    if args.expect != eligible_n:
        print(f"WARNING: --expect {args.expect} does not match the {eligible_n} eligible "
              f"rows just gathered. Aborting so you can re-review. Nothing purged.",
              file=sys.stderr)
        return 1

    logger.info("event=legacy_backlog_purge_run expect=%d", args.expect)
    result = purge_expired_cancellations(cutoff_days=_CUTOFF_DAYS, max_per_run=args.expect)
    print(f"[purge] result={result}")

    if result.get("status") == "cap_exceeded":
        print("Eligible count exceeded the cap (drift upward since the dry-run). "
              "Nothing was purged. Re-review and re-run.", file=sys.stderr)
        return 1
    if result.get("status") != "purged":
        print(f"Sweep returned status={result.get('status')}. No purge completed.",
              file=sys.stderr)
        return 1

    print(f"[purge] done. purged={result.get('purged')} failed={result.get('failed')}. "
          "Re-read POST /admin/audit-snapshot to confirm "
          "canceled_users_in_privacy_breach has moved toward 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
