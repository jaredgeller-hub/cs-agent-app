-- migrations/007_backfill_legacy_canceled_at.sql
--
-- One-time backfill of canceled_at on the pre-pilot trial-origin backlog:
-- rows canceled (subscription_status='canceled') BEFORE Step 2 shipped, so
-- they carry canceled_at = NULL and are invisible to the recurring cancel-
-- purge sweep (purge_expired_cancellations gathers only canceled_at IS NOT
-- NULL). This stamps canceled_at = trial_ends_at on the trial-origin subset
-- so those rows enter the normal purge lifecycle: rows already past the
-- 29-day cutoff are then closed by the one-time run of
-- scripts/purge_legacy_canceled_backlog.py, and any rows under 29 days are
-- left for the recurring sweep to catch as they age (still inside the 30-day
-- policy ceiling).
--
-- Scope is trial-origin ONLY, matching the purge exclusions exactly:
--   subscription_status = 'canceled'   the backlog
--   canceled_at IS NULL                not already stamped
--   stripe_subscription_id IS NULL     trial-origin; paid-origin rows are
--                                      handled by hand, NEVER auto-backfilled
--   trial_ends_at IS NOT NULL          a real timestamp exists to copy
--   is_comp_account = FALSE            never touch comp / the reviewer-test
--                                      canary; the purge excludes comp anyway
--   clerk_user_id != 'sarah_c'         demo identity; the primitive refuses it
--
-- Idempotent: re-running is a no-op because canceled_at is no longer NULL on
-- the affected rows after the first run.
--
-- Run on Render Postgres AFTER reviewing the read-only counts and the per-row
-- dry-run, and BEFORE running the purge script:
--   psql $DATABASE_URL -f migrations/007_backfill_legacy_canceled_at.sql
--
-- Rollback (only meaningful in the window BEFORE the purge runs; after the
-- purge the rows are gone). Safe ONLY if the pre-backfill audit confirmed
-- zero rows already had canceled_at set:
--   UPDATE customers SET canceled_at = NULL
--   WHERE subscription_status = 'canceled'
--     AND stripe_subscription_id IS NULL
--     AND is_comp_account = FALSE
--     AND clerk_user_id != 'sarah_c'
--     AND canceled_at IS NOT NULL;

UPDATE customers
SET    canceled_at = trial_ends_at
WHERE  subscription_status = 'canceled'
  AND  canceled_at IS NULL
  AND  stripe_subscription_id IS NULL
  AND  trial_ends_at IS NOT NULL
  AND  is_comp_account = FALSE
  AND  clerk_user_id != 'sarah_c';
