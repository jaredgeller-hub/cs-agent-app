-- Backfill existing customers with subscription_status='none' and no trial_ends_at
-- to a 14-day trial starting now. Idempotent (WHERE clause prevents re-runs from
-- overwriting already-trialing users).
--
-- Run on Render Postgres after deploying the trial provisioning code:
--   psql $DATABASE_URL -f migrations/001_backfill_trial_status.sql

UPDATE customers
SET    subscription_status = 'trialing',
       trial_ends_at = NOW() + INTERVAL '14 days'
WHERE  subscription_status = 'none'
  AND  trial_ends_at IS NULL
  AND  clerk_user_id != 'sarah_c';
