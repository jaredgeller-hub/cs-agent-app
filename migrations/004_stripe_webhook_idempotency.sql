-- Stripe webhook fail-CLOSED hardening: idempotency table + cancel intent column.
--
-- Creates stripe_webhook_events for INSERT-then-process idempotency on the
-- webhook handler at POST /billing/webhook (alias /webhooks/stripe). The
-- event_id PRIMARY KEY is Stripe's evt_* identifier; a duplicate INSERT
-- (IntegrityError) on redelivery is the signal that the event was already
-- processed and the handler should return 200 without reprocessing.
--
-- Adds cancel_at_period_end to customers so customer.subscription.updated
-- can record the user's cancel intent without flipping subscription_status.
-- Industry-standard B2B SaaS semantics: user retains access through the
-- paid period; subscription_status flips to 'canceled' only when
-- customer.subscription.deleted fires at the end of that period.
--
-- Idempotent (CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS),
-- safe to re-run. product/database.py init_db() also runs the customers
-- ALTER inline on every boot and Base.metadata.create_all picks up the
-- table for fresh deploys / test envs, so this file is the documented
-- record rather than the only path to the new schema.
--
-- Run on Render Postgres after deploying the schema commit:
--   psql $DATABASE_URL -f migrations/004_stripe_webhook_idempotency.sql
--
-- Rollback (if needed, in reverse order):
--   ALTER TABLE customers DROP COLUMN cancel_at_period_end;
--   DROP TABLE stripe_webhook_events;
--
-- NOTE: In production, this file was preceded by Base.metadata.create_all
-- which created the table with naive TIMESTAMP. An inline ALTER COLUMN
-- TYPE block in product/database.py init_db() (added in COMMIT 2)
-- corrects this on next boot. Re-running this .sql file remains safe but
-- is no longer needed; the inline ALTER is the authoritative source.

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    event_id          TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at      TIMESTAMPTZ,
    processing_status TEXT NOT NULL DEFAULT 'pending'
);

ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE;
