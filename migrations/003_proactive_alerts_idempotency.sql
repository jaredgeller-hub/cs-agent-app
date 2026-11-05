-- Phase 5 (Track G): idempotency for F1 absence-detection alert writes.
-- Adds two nullable columns and a partial UNIQUE index that fires only
-- when both columns are populated. Postcall writes leave both columns
-- NULL and are unaffected — postcall dedup design lands Sprint 6+.
--
-- Run on Render Postgres after deploying the schema commit:
--   psql $DATABASE_URL -f migrations/003_proactive_alerts_idempotency.sql
--
-- Rollback (if needed, in reverse order):
--   DROP INDEX proactive_alerts_dedup_unique;
--   ALTER TABLE proactive_alerts DROP COLUMN alert_date;
--   ALTER TABLE proactive_alerts DROP COLUMN signal_id;

ALTER TABLE proactive_alerts ADD COLUMN signal_id VARCHAR;
ALTER TABLE proactive_alerts ADD COLUMN alert_date DATE;

CREATE UNIQUE INDEX proactive_alerts_dedup_unique
ON proactive_alerts (csm_id, account, signal_id, alert_date)
WHERE signal_id IS NOT NULL AND alert_date IS NOT NULL;
