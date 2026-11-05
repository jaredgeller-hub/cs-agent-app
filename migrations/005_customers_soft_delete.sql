-- Customers soft-delete support. Adds is_deleted + deleted_at so an
-- orphaned or abandoned customer row (e.g. the Clerk user was deleted
-- upstream, or a signup never completed onboarding) can be marked
-- inactive without a hard DELETE.
--
-- A soft-deleted row (is_deleted = TRUE) is excluded from:
--   - proactive-alert fan-out  (product/org_config.py list_proactive_eligible_csms)
--   - trial engagement emails   (main.py _process_trial_emails)
-- No row is ever hard-deleted by this migration.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS), safe to re-run. As of this
-- migration there are ZERO orphaned/abandoned rows in production — every
-- customers row has a non-null clerk_user_id and onboarding_complete = true
-- (verified 2026-05-21) — so this ships as preventive infrastructure with
-- no backfill UPDATE. Marking a row deleted is a deliberate manual op:
--   UPDATE customers SET is_deleted = TRUE, deleted_at = NOW() WHERE clerk_user_id = '...';
--
-- product/database.py init_db() also runs these ALTERs inline on every
-- boot (ADD COLUMN IF NOT EXISTS) and Base.metadata.create_all picks the
-- columns up for fresh deploys / test envs, so this file is the documented
-- record rather than the only path to the new schema.
--
-- Run on Render Postgres after deploying the schema commit:
--   psql $DATABASE_URL -f migrations/005_customers_soft_delete.sql
--
-- Rollback (if needed, in reverse order):
--   ALTER TABLE customers DROP COLUMN deleted_at;
--   ALTER TABLE customers DROP COLUMN is_deleted;

ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;
