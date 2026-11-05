"""scripts/backfill_clerk_emails.py

One-shot backfill that fixes customer rows whose email is the
@noreply.clerk placeholder by fetching the real email and name from
the Clerk Backend API. After updating each row, deletes any
engagement_sends rows so the trial engagement cron re-attempts emails
that were previously marked sent against an unsendable address.

Idempotent: rows that no longer match @noreply.clerk are skipped on a
second run. Rate-limited: 250 ms between Clerk API calls.

Usage:
    python3 scripts/backfill_clerk_emails.py --dry-run
    python3 scripts/backfill_clerk_emails.py

Logging policy: never logs PII (real email, name) at INFO. Counts and
opaque clerk_user_id values only.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_clerk_emails")

CLERK_API_SLEEP_S = 0.25

import product.database as _db
from product.database import Customer, EngagementSend
from product.auth_clerk import fetch_clerk_user_profile, is_noreply_email


def run_backfill(dry_run: bool) -> int:
    """Execute the backfill. Returns process exit code (0 on success).

    product.database declares SessionLocal=None at module load and only
    assigns it inside init_db(), which the FastAPI app calls at startup.
    Standalone scripts must call init_db() themselves. We only do so when
    SessionLocal is currently None so test fixtures that pre-bind a
    sessionmaker into _db.SessionLocal are not clobbered.
    """
    if _db.SessionLocal is None:
        if not _db.DATABASE_URL:
            logger.error("DATABASE_URL env var not set; aborting")
            return 2
        if not _db.init_db():
            logger.error(
                "Database initialization failed; check DATABASE_URL value "
                "and Postgres connectivity"
            )
            return 2
        if _db.SessionLocal is None:
            logger.error("SessionLocal unexpectedly None after init_db(); aborting")
            return 2

    SessionLocal = _db.SessionLocal

    scanned = 0
    would_update = 0
    skipped_no_clerk_email = 0
    skipped_no_clerk_id = 0
    engagement_rows_cleared = 0

    session = SessionLocal()
    try:
        rows = session.query(Customer).filter(
            Customer.email.like("%@noreply.clerk")
        ).all()
        scanned = len(rows)
        logger.info("Found %d customer rows with @noreply.clerk placeholder", scanned)

        for cust in rows:
            clerk_user_id = (cust.clerk_user_id or "").strip()
            if not clerk_user_id:
                logger.warning(
                    "Customer id=%s has no clerk_user_id; skipping",
                    cust.id,
                )
                skipped_no_clerk_id += 1
                continue

            profile = fetch_clerk_user_profile(clerk_user_id)
            time.sleep(CLERK_API_SLEEP_S)

            if not profile or not profile.get("email"):
                logger.warning(
                    "Clerk has no primary email for clerk_user_id=%s; leaving row as-is",
                    clerk_user_id,
                )
                skipped_no_clerk_email += 1
                continue

            new_email = profile["email"]
            if is_noreply_email(new_email):
                # Defensive: Clerk should never return a noreply-shaped
                # email, but if it did we'd loop forever.
                logger.warning(
                    "Clerk returned noreply-shaped email for clerk_user_id=%s; skipping",
                    clerk_user_id,
                )
                skipped_no_clerk_email += 1
                continue

            new_name = profile.get("name") or None
            pending_engagement = (
                session.query(EngagementSend)
                .filter_by(csm_id=clerk_user_id)
                .count()
            )

            if dry_run:
                logger.info(
                    "[DRY] WOULD update clerk_user_id=%s "
                    "(set_email=true set_name=%s clear_engagement_rows=%d)",
                    clerk_user_id,
                    "true" if (new_name and not cust.name) else "false",
                    pending_engagement,
                )
                would_update += 1
                engagement_rows_cleared += pending_engagement
                continue

            cust.email = new_email
            if new_name and not cust.name:
                cust.name = new_name
            cleared = (
                session.query(EngagementSend)
                .filter_by(csm_id=clerk_user_id)
                .delete()
            )
            session.commit()
            would_update += 1
            engagement_rows_cleared += cleared
            logger.info(
                "Updated customer clerk_user_id=%s; cleared %d engagement_sends rows",
                clerk_user_id, cleared,
            )

    except Exception as exc:
        session.rollback()
        logger.error("Backfill failed: %s", exc)
        return 1
    finally:
        session.close()

    logger.info(
        "Summary: scanned=%d updated=%d skipped_no_clerk_email=%d "
        "skipped_no_clerk_id=%d engagement_rows_cleared=%d dry_run=%s",
        scanned, would_update, skipped_no_clerk_email,
        skipped_no_clerk_id, engagement_rows_cleared, dry_run,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill customer emails from Clerk for @noreply.clerk rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing to the database.",
    )
    args = parser.parse_args()
    return run_backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
