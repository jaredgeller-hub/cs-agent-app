-- 008_processed_zoom_recordings.sql
--
-- Idempotency ledger for Zoom recording.completed webhook processing.
-- The Zoom webhook can fire more than once for the same recording, so
-- job_process_zoom_recording writes a row here after a successful recap
-- and treats the UNIQUE (csm_id, recording_id) constraint as the dedup
-- guard (catch IntegrityError, return silently).
--
-- Apply manually via psql before the deploy that ships the Zoom build.
-- Not auto-applied. create_all() picks up the matching ORM model on
-- fresh deploys and test envs; this migration is for the live Postgres.

CREATE TABLE IF NOT EXISTS processed_zoom_recordings (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    csm_id          TEXT NOT NULL,
    recording_id    TEXT NOT NULL,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (csm_id, recording_id)
);
