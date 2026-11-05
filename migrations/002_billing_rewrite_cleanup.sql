-- Billing rewrite data migration. Idempotent, safe to re-run.
-- Run on Render Postgres after deploying the billing rewrite:
--   psql $DATABASE_URL -f migrations/002_billing_rewrite_cleanup.sql

-- 1. Convert 'none' status to 'trialing' with a 14-day trial.
--    Does NOT touch 'canceled', 'active', or existing 'trialing' rows.
UPDATE customers
SET    subscription_status = 'trialing',
       trial_ends_at = NOW() + INTERVAL '14 days'
WHERE  subscription_status = 'none'
  AND  clerk_user_id != 'sarah_c';

-- 2. Safety: any 'trialing' row missing trial_ends_at gets one.
UPDATE customers
SET    trial_ends_at = NOW() + INTERVAL '14 days'
WHERE  subscription_status = 'trialing'
  AND  trial_ends_at IS NULL
  AND  clerk_user_id != 'sarah_c';

-- 3. Verify: no 'none' status should remain (except sarah_c demo).
SELECT clerk_user_id, email, subscription_status, trial_ends_at
FROM   customers
WHERE  subscription_status NOT IN ('trialing', 'active', 'canceled')
  AND  clerk_user_id != 'sarah_c';
