"""
product/database.py

SQLAlchemy ORM layer for customers, connections, and digest logs.
DATABASE_URL is injected by Render's linked Postgres instance.
All calls are additive — existing file-based config (org_config.json) is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("cs-agent.database")

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Index, Integer, JSON, String, Text,
    UniqueConstraint, create_engine, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL")

# Render injects postgres:// but SQLAlchemy requires postgresql://
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

Base = declarative_base()


def as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a datetime to UTC-aware. No-op if already aware, None-safe.

    SQLAlchemy DateTime columns without `timezone=True` return naive
    datetimes on some backends (SQLite in-memory used by tests) and
    aware datetimes on others (Postgres in production via psycopg2).
    Callers comparing column values to `datetime.now(timezone.utc)` would
    hit TypeError on the naive case. Wrapping reads with as_utc() makes
    the comparison safe in both environments.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class Customer(Base):
    __tablename__ = "customers"

    id                     = Column(String, primary_key=True)   # internal UUID
    email                  = Column(String, nullable=False, unique=True)
    name                   = Column(String)
    clerk_user_id          = Column(String, unique=True)
    stripe_customer_id     = Column(String)
    stripe_subscription_id = Column(String)
    subscription_status    = Column(String, default="trialing")  # trialing | active | canceled
    trial_ends_at          = Column(DateTime)
    onboarding_complete    = Column(Boolean, default=False)
    accounts               = Column(JSON)                        # list of account name strings
    timezone               = Column(String)                       # IANA timezone (e.g. "America/Chicago")
    created_at             = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at          = Column(DateTime)
    # A.1 progressive unlock. Server-side flag gating real Google write
    # actions (Gmail send + Calendar event create/update/delete). Default
    # False; flipped to True by /app/settings/google-actions once the
    # user grants BOTH gmail.send AND calendar.events via reauth.
    # Independent from subscription status by design — trial users unlock
    # write actions during the 14-day window.
    google_actions_enabled = Column(Boolean, default=False)
    # C.1 Commit 7: CSM-chosen cadence for the post-call transparent nudge
    # (flow C in product/postcall/scheduler.py). One of {5, 15, 30, 60}
    # minutes; null → treat as default 15. Flow A (Drive transcript found)
    # and flow B-skip (CRM already logged it) are not gated by cadence.
    postcall_nudge_cadence_minutes = Column(Integer, nullable=True)
    # Per-user override for the billing gate. True bypasses the
    # subscription/trial check at both gate paths (the dependency at
    # main.py _require_active_subscription and /chat's inline check).
    # Used for design partners, advisors, pilots, and reviewer test
    # accounts that need access without a Stripe subscription. Single
    # source of truth: this column. Read by is_subscription_active.
    is_comp_account = Column(Boolean, default=False, nullable=False)
    # Records Stripe's cancel_at_period_end intent from
    # customer.subscription.updated. True means the user has chosen to
    # cancel but retains access until customer.subscription.deleted
    # fires at the end of the paid period. subscription_status stays
    # 'active' while this is True; the gate is unchanged.
    cancel_at_period_end = Column(Boolean, default=False, nullable=False)
    # Records the timestamp when the subscription actually ends, based
    # on Stripe's current_period_end field on the Subscription object.
    # Null when cancel_at_period_end is False (no scheduled end date).
    # Populated alongside cancel_at_period_end=True from
    # customer.subscription.updated webhooks. Used by the in-app UX
    # to show "your subscription cancels on March 28" without needing
    # a live Stripe API call.
    subscription_cancels_at = Column(DateTime)
    # Soft-delete support (I.2). is_deleted = True marks an orphaned or
    # abandoned customer row — the Clerk user was deleted upstream, or a
    # signup never completed — without a hard DELETE. Soft-deleted rows are
    # excluded from proactive-alert fan-out (list_proactive_eligible_csms)
    # and the trial engagement email job (_process_trial_emails). Default
    # False; flipped manually via psql. deleted_at records when the
    # soft-delete happened (nullable; null while is_deleted is False).
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    # canceled_at records when a subscription actually ended — set by the
    # cancel webhook in Step 2 (customer.subscription.deleted), currently
    # unset everywhere. Distinct from subscription_cancels_at (the scheduled
    # future end-date from cancel_at_period_end) and from deleted_at (the
    # soft-delete marker). Nullable; null until a cancellation fires. Used by
    # the 30-day cancel-purge sweep to find subscriptions past the retention
    # window.
    canceled_at = Column(DateTime, nullable=True)


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("customer_id", "provider", name="uq_connection_customer_provider"),
    )

    id               = Column(String, primary_key=True)   # UUID
    # stores either Clerk user_id or Customer.id depending on write path
    # NOT enforced as FK; lookup code should try both keys
    customer_id      = Column(String, nullable=False)
    provider         = Column(String, nullable=False)     # gmail | calendar | hubspot
    token_data       = Column(JSON)                        # token dict (store encrypted in prod)
    scopes_granted   = Column(String)
    status           = Column(String, default="active")   # active | expired | revoked
    last_verified_at = Column(DateTime)
    created_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class EditDelta(Base):
    __tablename__ = "edit_deltas"

    id               = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    csagent_user_id  = Column(String, nullable=False)
    draft_type       = Column(String, nullable=False)
    account_name     = Column(String)
    original_text    = Column(Text, nullable=False)
    edited_text      = Column(Text, nullable=False)
    was_edited       = Column(Boolean, nullable=False)
    summarized_at    = Column(DateTime)
    created_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserPreference(Base):
    __tablename__ = "user_preferences"

    csagent_user_id  = Column(String, primary_key=True)
    preference_profile = Column(Text)
    updated_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserContextSnapshot(Base):
    __tablename__ = "user_context_snapshots"

    csm_id          = Column(String, primary_key=True)
    context_json    = Column(Text, nullable=False)
    refreshed_at    = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    refresh_status  = Column(String, default="pending")  # pending | refreshing | complete | failed


class EngagementSend(Base):
    __tablename__ = "engagement_sends"

    id      = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    csm_id  = Column(String, nullable=False)
    day     = Column(Integer, nullable=False)
    sent_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Unique on (csm_id, day) enforced in application logic


class ProactiveAlert(Base):
    __tablename__ = "proactive_alerts"
    __table_args__ = (
        # Phase 5 (Track G) idempotency: F1 absence detection writes
        # populate signal_id + alert_date, and the partial unique index
        # below catches duplicates at the DB layer. Postcall writes
        # leave both NULL — the WHERE clause excludes them so postcall
        # remains unconstrained until Sprint 6+ dedup design lands.
        # Production gets this index via migrations/003 against the
        # existing table; create_all() picks it up for fresh deploys
        # and test envs.
        Index(
            "proactive_alerts_dedup_unique",
            "csm_id", "account", "signal_id", "alert_date",
            unique=True,
            postgresql_where=text("signal_id IS NOT NULL AND alert_date IS NOT NULL"),
            sqlite_where=text("signal_id IS NOT NULL AND alert_date IS NOT NULL"),
        ),
    )

    id                   = Column(String, primary_key=True)   # UUID from alert_event["event_id"]
    csm_id               = Column(String, nullable=False)
    account              = Column(String)
    alert_text           = Column(Text, nullable=False)
    draft_text           = Column(Text)
    # Populated by C.1 post-call scheduler (Commits 5+6). JSON-encoded dict
    # matching the frontend's alert.postcall_draft shape:
    # {recap, draft_text, account_name, account_crm_id, crm_type,
    # drafted_event_id}. Null for non-post-call alerts.
    postcall_draft_json  = Column(Text)
    urgency              = Column(String)
    created_at           = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Populated by F1 absence detection writes only. Postcall writes
    # leave these NULL until the postcall dedup design lands (Sprint 6+).
    # The partial unique index proactive_alerts_dedup_unique only fires
    # when both columns are NOT NULL — see migration 003.
    signal_id            = Column(String)
    alert_date           = Column(Date)


class ProcessedZoomRecording(Base):
    """Idempotency ledger for Zoom recording.completed webhook processing.

    The Zoom webhook can fire more than once for the same recording. The
    Zoom recording job writes a row here after a successful recap and
    treats the UNIQUE (csm_id, recording_id) constraint as the dedup
    guard - a duplicate INSERT raises IntegrityError and the job returns
    silently (same posture as the Stripe webhook idempotency table).

    Production gets this table via migrations/008; create_all() picks it
    up for fresh deploys and test envs.
    """
    __tablename__ = "processed_zoom_recordings"
    __table_args__ = (
        UniqueConstraint("csm_id", "recording_id", name="uq_processed_zoom_csm_recording"),
    )

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    csm_id        = Column(String, nullable=False)
    recording_id  = Column(String, nullable=False)
    processed_at  = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state         = Column(String, primary_key=True)
    provider      = Column(String, nullable=False)       # google | hubspot | gainsight | slack | zoom
    csm_id        = Column(String, nullable=False)
    code_verifier = Column(String)                        # PKCE code_verifier (null for Google)
    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at    = Column(DateTime, nullable=False)      # created_at + 10 minutes


class SignalCache(Base):
    __tablename__ = "signal_cache"
    __table_args__ = (
        Index(
            "ix_signal_cache_csm_type_refreshed",
            "csm_id", "signal_type", "refreshed_at",
        ),
    )

    csm_id         = Column(String, primary_key=True)
    signal_type    = Column(String, primary_key=True)   # email.absence | email.received | email.new_stakeholder | ...
    signal_id      = Column(String, primary_key=True)   # "{thread_id}:{contact}:{account}" for gmail
    signal_payload = Column(JSON, nullable=False)        # full signal dict verbatim
    refreshed_at   = Column(DateTime, nullable=False,
                            default=lambda: datetime.now(timezone.utc))


class StripeWebhookEvent(Base):
    """Idempotency record for processed Stripe webhook events.

    event_id is Stripe's evt_* identifier and serves as the unique
    constraint. INSERT-then-process: the handler inserts a row before
    dispatching; a duplicate INSERT (IntegrityError) means Stripe
    redelivered an event we already saw, and the handler returns 200
    without reprocessing.

    processing_status values:
      pending                  - row inserted, dispatch not yet finished
      processed                - transition function returned True
      skipped_no_handler       - event_type has no branch (default else)
      skipped_no_customer_row  - transition returned False (no row found)
      failed_will_retry        - transition raised; Stripe will redeliver
      failed_terminal          - reserved for future terminal-failure path
    """

    __tablename__ = "stripe_webhook_events"

    event_id          = Column(String, primary_key=True)   # Stripe evt_*
    event_type        = Column(String, nullable=False)
    received_at       = Column(DateTime(timezone=True), nullable=False,
                               default=lambda: datetime.now(timezone.utc))
    processed_at      = Column(DateTime(timezone=True))     # null until terminal
    processing_status = Column(String, nullable=False, default="pending")


class DigestLog(Base):
    __tablename__ = "digest_logs"

    id               = Column(String, primary_key=True)   # UUID
    customer_id      = Column(String, nullable=False)
    date             = Column(String, nullable=False)      # ISO date string
    status           = Column(String, nullable=False)      # success | failed | skipped
    signal_count     = Column(Integer, default=0)
    gmail_signals    = Column(Integer, default=0)
    calendar_signals = Column(Integer, default=0)
    hubspot_signals  = Column(Integer, default=0)
    error_message    = Column(Text)
    digest_text      = Column(Text)                        # full digest content for cross-service access
    delivered_at     = Column(DateTime)
    created_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------

engine: object = None
SessionLocal: object = None


def init_db() -> bool:
    """Create tables and initialise the session factory. Call once on startup."""
    global engine, SessionLocal
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set — database disabled")
        return False
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5)
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    # Safe migration: add digest_text column if missing (existing rows won't have it)
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE digest_logs ADD COLUMN IF NOT EXISTS digest_text TEXT"))
            conn.commit()
    except Exception:
        pass  # column already exists or DB doesn't support IF NOT EXISTS
    # Safe migration: add unique index on (customer_id, provider) in connections.
    # create_all above won't add constraints to pre-existing tables, so we issue
    # the index DDL explicitly. Fails if duplicate rows already exist — investigate
    # prod data before relying on the upsert path if this logs an error upstream.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_connection_customer_provider "
                "ON connections (customer_id, provider)"
            ))
            conn.commit()
    except Exception:
        pass
    # Promote the existing unique index to a named UNIQUE constraint so
    # pg_insert(...).on_conflict_do_update(constraint=...) can reference it
    # by name on older deployments. Skip if the constraint already exists
    # (avoids noisy error log on every startup).
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            _exists = conn.execute(text(
                "SELECT 1 FROM pg_constraint WHERE conname = 'uq_connection_customer_provider'"
            )).fetchone()
            if not _exists:
                conn.execute(text(
                    "ALTER TABLE connections ADD CONSTRAINT uq_connection_customer_provider "
                    "UNIQUE USING INDEX uq_connection_customer_provider"
                ))
            conn.commit()
    except Exception:
        pass
    # Safe migration: add timezone column to customers
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE customers ADD COLUMN IF NOT EXISTS timezone VARCHAR"))
            conn.commit()
    except Exception:
        pass
    # Safe migration: rename gmail_send_enabled → google_actions_enabled
    # (A.1 Commit 5). Idempotent across fresh deploy (creates new column),
    # upgrade deploy (renames old column preserving default + type + any
    # existing False values), and replay deploy (both steps no-op).
    # SQLite in tests lacks information_schema and falls into the except;
    # tests get google_actions_enabled via Base.metadata.create_all after
    # the ORM model update above.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='customers' "
                "AND column_name='gmail_send_enabled'"
            )).fetchone()
            if result:
                conn.execute(text(
                    "ALTER TABLE customers "
                    "RENAME COLUMN gmail_send_enabled "
                    "TO google_actions_enabled"
                ))
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
                "google_actions_enabled BOOLEAN DEFAULT FALSE"
            ))
            conn.commit()
    except Exception:
        pass
    # Safe migration: add postcall_draft_json to proactive_alerts (C.1 Commit 5+6)
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE proactive_alerts ADD COLUMN IF NOT EXISTS postcall_draft_json TEXT"
            ))
            conn.commit()
    except Exception:
        pass
    # Safe migration: add postcall_nudge_cadence_minutes to customers (C.1 Commit 7)
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS postcall_nudge_cadence_minutes INTEGER"
            ))
            conn.commit()
    except Exception:
        pass
    # stripe_webhook_events timestamp columns landed as naive on first
    # boot because create_all preceded migrations/004_*.sql. Bring them
    # into alignment with the documented migration. ALTER COLUMN TYPE
    # is a no-op rewrite on an empty table (verified before COMMIT 2).
    # Safe to re-run; idempotent because Postgres treats "change type
    # to current type" as zero work.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE stripe_webhook_events "
                "ALTER COLUMN received_at TYPE TIMESTAMPTZ "
                "USING received_at AT TIME ZONE 'UTC'"
            ))
            conn.execute(text(
                "ALTER TABLE stripe_webhook_events "
                "ALTER COLUMN processed_at TYPE TIMESTAMPTZ "
                "USING processed_at AT TIME ZONE 'UTC'"
            ))
            conn.commit()
    except Exception as exc:
        logger.warning(
            "stripe_webhook_events TIMESTAMPTZ migration failed: %s", exc
        )
    # Safe migration: add is_comp_account to customers (billing gate override)
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
                "is_comp_account BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.commit()
    except Exception:
        pass
    # Safe migration: add cancel_at_period_end to customers
    # (Stripe webhook fail-CLOSED hardening — records cancel intent
    # from customer.subscription.updated without flipping access).
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
                "cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.commit()
    except Exception:
        pass
    # Safe migration: add subscription_cancels_at to customers
    # (COMMIT 4 — records when scheduled cancellation takes effect).
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
                "subscription_cancels_at TIMESTAMP"
            ))
            conn.commit()
    except Exception:
        pass
    # Safe migration: add soft-delete columns to customers (I.2).
    # Authoritative path — migrations/005 is the documented record but
    # this inline ALTER lands the columns on next boot so the filtered
    # queries (list_proactive_eligible_csms, _process_trial_emails) never
    # reference a column that does not yet exist in production.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
                "is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
                "deleted_at TIMESTAMP"
            ))
            conn.commit()
    except Exception:
        pass
    # Safe migration: add canceled_at to customers. Records when a
    # subscription actually ended (set by the cancel webhook in Step 2,
    # currently unset). migrations/006 is the documented record; this inline
    # ALTER lands the column on next boot so the 30-day cancel-purge sweep
    # never references a column that does not yet exist in production.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
                "canceled_at TIMESTAMP"
            ))
            conn.commit()
    except Exception:
        pass
    logger.info("Database initialized")
    return True


def get_db():
    """Yield a database session. Commits on clean exit, rolls back on exception."""
    if not SessionLocal:
        return None
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def update_webhook_event_status(event_id: str, status: str) -> None:
    """Update processing_status on the stripe_webhook_events row.

    Best-effort: logs warning on failure but does not raise. The Stripe
    webhook handler must not 500 because an audit-trail update failed;
    the actual processing already succeeded by the time this is called.

    Sets processed_at to now() only when status is 'processed'. Other
    statuses (skipped_*, failed_*) leave processed_at NULL so the audit
    trail distinguishes 'ran the transition and it succeeded' from
    'handler completed without running a transition'.
    """
    try:
        if not SessionLocal:
            logger.warning(
                "update_webhook_event_status: SessionLocal unset, event_id=%s",
                event_id,
            )
            return
        session = SessionLocal()
        try:
            row = session.query(StripeWebhookEvent).filter_by(event_id=event_id).first()
            if row is None:
                logger.warning(
                    "update_webhook_event_status: event_id=%s not found",
                    event_id,
                )
                return
            row.processing_status = status
            if status == "processed":
                row.processed_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
    except Exception as exc:
        logger.warning(
            "update_webhook_event_status failed for event_id=%s: %s",
            event_id, exc,
        )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_or_create_customer(clerk_user_id: str, email: str | None, name: str = None) -> dict | None:
    """Upsert a customer record by Clerk user ID (idempotent, race-safe).

    Uses a single INSERT ... ON CONFLICT (clerk_user_id) DO UPDATE so concurrent
    requests for the same user never produce a UniqueViolation.  If email is
    absent, a synthetic unique address is used to satisfy the NOT NULL + UNIQUE
    constraint on that column.

    New users get a 14-day free trial (subscription_status='trialing',
    trial_ends_at=now+14d). Existing users only get last_login_at updated.
    sarah_c (demo identity) is excluded from trial provisioning.

    F15: when the caller did not supply an email (or supplied the
    @noreply.clerk placeholder) we fetch the real email and name from
    the Clerk Backend API before writing the row. Mirrors the same guard
    in product.subscription.provision_customer_on_first_auth.
    """
    if not SessionLocal:
        return None
    # F15: backfill missing email/name from the Clerk Backend API.
    if email is None or (email and "@noreply.clerk" in email):
        try:
            from product.auth_clerk import fetch_clerk_user_profile
            profile = fetch_clerk_user_profile(clerk_user_id)
        except Exception:
            profile = None
        if profile and profile.get("email"):
            email = profile["email"]
            if not name and profile.get("name"):
                name = profile["name"]
    # Fallback so every row has a unique, non-empty email value
    effective_email = email if email else f"{clerk_user_id}@noreply.clerk"

    # Demo bypass: sarah_c gets "none" status, no trial
    from product.demo_identity import is_demo_csm_id, is_demo_email
    _is_demo = is_demo_csm_id(clerk_user_id) or is_demo_email(email)
    _initial_status = "none" if _is_demo else "trialing"
    _trial_end = None if _is_demo else (datetime.now(timezone.utc) + timedelta(days=14))

    session = SessionLocal()
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = (
            pg_insert(Customer)
            .values(
                id=str(uuid.uuid4()),
                email=effective_email,
                name=name,
                clerk_user_id=clerk_user_id,
                subscription_status=_initial_status,
                trial_ends_at=_trial_end,
                created_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=["clerk_user_id"],
                set_=dict(
                    last_login_at=datetime.now(timezone.utc),
                    **({"name": name} if name else {}),
                ),
            )
        )
        session.execute(stmt)
        session.commit()
        customer = session.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        return {
            "id": customer.id,
            "email": customer.email,
            "name": customer.name,
            "clerk_user_id": customer.clerk_user_id,
            "subscription_status": customer.subscription_status,
            "onboarding_complete": customer.onboarding_complete,
            "accounts": customer.accounts,
        }
    finally:
        session.close()


def save_connection(customer_id: str, provider: str, token_data: dict, scopes: str = None):
    """Upsert a provider connection (Gmail, Calendar, HubSpot)."""
    if not SessionLocal:
        return
    session = SessionLocal()
    try:
        conn = session.query(Connection).filter_by(
            customer_id=customer_id, provider=provider
        ).first()
        if conn:
            conn.token_data = token_data
            conn.status = "active"
            conn.last_verified_at = datetime.now(timezone.utc)
        else:
            conn = Connection(
                id=str(uuid.uuid4()),
                customer_id=customer_id,
                provider=provider,
                token_data=token_data,
                scopes_granted=scopes,
                status="active",
                last_verified_at=datetime.now(timezone.utc),
            )
            session.add(conn)
        session.commit()
    finally:
        session.close()


def get_connection(customer_id: str, provider: str) -> dict | None:
    """Return token data for a provider connection, or None if not found."""
    if not SessionLocal:
        return None
    session = SessionLocal()
    try:
        conn = session.query(Connection).filter_by(
            customer_id=customer_id, provider=provider
        ).first()
        if conn:
            return {"token_data": conn.token_data, "status": conn.status}
        return None
    finally:
        session.close()


def log_digest(
    customer_id: str,
    date: str,
    status: str,
    signal_count: int = 0,
    gmail_signals: int = 0,
    calendar_signals: int = 0,
    hubspot_signals: int = 0,
    error_message: str = None,
):
    """Record a digest delivery attempt."""
    if not SessionLocal:
        return
    session = SessionLocal()
    try:
        log = DigestLog(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            date=date,
            status=status,
            signal_count=signal_count,
            gmail_signals=gmail_signals,
            calendar_signals=calendar_signals,
            hubspot_signals=hubspot_signals,
            error_message=error_message,
            delivered_at=datetime.now(timezone.utc) if status == "success" else None,
        )
        session.add(log)
        session.commit()
    finally:
        session.close()


def update_customer_subscription(
    email: str,
    stripe_customer_id: str = None,
    stripe_subscription_id: str = None,
    subscription_status: str = None,
    trial_ends_at: datetime = None,
):
    """Update subscription fields from a Stripe webhook event."""
    if not SessionLocal:
        return
    session = SessionLocal()
    try:
        customer = session.query(Customer).filter_by(email=email).first()
        if customer is None:
            logger.warning("Stripe webhook for email=%s found no matching customer in Postgres", email)
            return
        if stripe_customer_id:
            customer.stripe_customer_id = stripe_customer_id
        if stripe_subscription_id:
            customer.stripe_subscription_id = stripe_subscription_id
        if subscription_status:
            customer.subscription_status = subscription_status
        if trial_ends_at:
            customer.trial_ends_at = trial_ends_at
        session.commit()
    finally:
        session.close()


def update_customer_onboarding(clerk_user_id: str, accounts: list, onboarding_complete: bool = True):
    """Persist account list and mark onboarding complete."""
    if not SessionLocal:
        return
    session = SessionLocal()
    try:
        customer = session.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        if customer:
            customer.accounts = accounts
            customer.onboarding_complete = onboarding_complete
            session.commit()
    finally:
        session.close()


def get_all_active_customers() -> list:
    """Return customers with active/trialing subscriptions who completed onboarding."""
    if not SessionLocal:
        return []
    session = SessionLocal()
    try:
        customers = session.query(Customer).filter(
            Customer.subscription_status.in_(["active", "trialing"]),
            Customer.onboarding_complete == True,
        ).all()
        return [
            {
                "id": c.id,
                "email": c.email,
                "name": c.name,
                "clerk_user_id": c.clerk_user_id,
                "accounts": c.accounts or [],
            }
            for c in customers
        ]
    finally:
        session.close()


def save_edit_delta(
    csagent_user_id: str,
    draft_type: str,
    original_text: str,
    edited_text: str,
    was_edited: bool,
    account_name: str = None,
):
    """Record an edit delta from draft approval (inward loop)."""
    if not SessionLocal:
        return
    session = SessionLocal()
    try:
        delta = EditDelta(
            id=str(uuid.uuid4()),
            csagent_user_id=csagent_user_id,
            draft_type=draft_type,
            account_name=account_name,
            original_text=original_text,
            edited_text=edited_text,
            was_edited=was_edited,
        )
        session.add(delta)
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Data purge (privacy-policy deletion primitive)
# ---------------------------------------------------------------------------
#
# purge_user_data is the single server-side routine that makes the privacy
# policy's two deletion promises real (the Delete-my-data button and the
# cancel-triggered 30-day purge). As of this commit it has NO live caller —
# Steps 2 (cancel sweep) and 3 (Delete button) wire it up later. Deploying
# this code is a runtime no-op until then.
#
# Design constraints (peer-reviewed, destructive data-ops):
#   - Fail-closed guard: never purge the demo protagonist, never run without
#     a DB, never run when no customers row matches.
#   - Literal key match only. customer_id / csm_id / csagent_user_id all hold
#     the SAME literal Clerk user id (confirmed by the connections audit:
#     nine user_* rows plus the bare-slug sarah_c, zero UUID-form rows). No
#     prefix matching, no clerk-or-UUID disjunction — match the literal value.
#   - All-or-nothing: every dependent delete plus the customers row commit in
#     ONE transaction. Any failure rolls back the whole thing; never partial.
#   - stripe_webhook_events is billing/accounting audit and is NEVER touched.
#   - On-disk cleanup is best-effort and runs AFTER the DB commit. Postgres is
#     the durable store of record; the product/data tree is a per-instance
#     cache, so on-disk failure only logs and never changes the return status.

# The nine dependent tables, in delete order, paired with the column that
# holds the literal Clerk user id. The customers root row is deleted last,
# separately (see purge_user_data).
_PURGE_TABLE_SPECS = [
    ("signal_cache", SignalCache, "csm_id"),
    ("digest_logs", DigestLog, "customer_id"),
    ("proactive_alerts", ProactiveAlert, "csm_id"),
    ("edit_deltas", EditDelta, "csagent_user_id"),
    ("user_preferences", UserPreference, "csagent_user_id"),
    ("user_context_snapshots", UserContextSnapshot, "csm_id"),
    ("engagement_sends", EngagementSend, "csm_id"),
    ("oauth_states", OAuthState, "csm_id"),
    ("connections", Connection, "customer_id"),
]


def _bulk_delete(session, model, column: str, value: str) -> int:
    """DELETE all rows of `model` where `column` == `value`; return the count.

    synchronize_session=False issues the DELETE directly to the DB without
    expiring in-session ORM objects — correct here because purge_user_data
    discards the session immediately after. The delete executes inside the
    caller's open transaction and is undone by rollback if a later delete
    raises. Isolated as a module-level seam so the atomicity test can force
    a mid-transaction failure on one table.
    """
    return (
        session.query(model)
        .filter(getattr(model, column) == value)
        .delete(synchronize_session=False)
    )


def _purge_on_disk(clerk_user_id: str) -> None:
    """Best-effort removal of this instance's product/data subtrees for a user.

    Never raises. Each target is wrapped independently so one failure does not
    block the others. The DB purge is the real guarantee; this is cache cleanup
    of the serving instance only (the tree is per-instance ephemeral, so no
    single request reaches every instance's copy).
    """
    try:
        from product.csm_id_validation import safe_data_path, safe_data_file
    except Exception as exc:
        logger.warning("purge on-disk: path helpers unavailable: %s", exc)
        return

    # (base_dir, is_dir). Directories are keyed by id; files are {id}.json.
    dir_targets = [
        "product/data/digests",
        "product/data/alerts",
        "product/data/feedback",
    ]
    file_targets = [
        "product/data/accounts",
        "product/data/postcall_nudges",
    ]
    for base in dir_targets:
        try:
            path = safe_data_path(base, clerk_user_id)
            shutil.rmtree(path, ignore_errors=True)
        except Exception as exc:
            logger.warning(
                "purge on-disk: failed to remove dir %s/%s: %s",
                base, clerk_user_id, exc,
            )
    for base in file_targets:
        try:
            path = safe_data_file(base, clerk_user_id)
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(
                "purge on-disk: failed to remove file %s/%s.json: %s",
                base, clerk_user_id, exc,
            )


def purge_user_data(clerk_user_id: str) -> dict:
    """Hard-delete all of a user's data across Postgres + the on-disk tree.

    See the module section comment above for the full contract. Returns a
    status dict: "not_purgeable" (guard refused, nothing deleted), "failed"
    (a delete raised, full rollback, nothing deleted), or "purged" (committed,
    with per-table rows_deleted counts). stripe_webhook_events is never
    touched. On-disk cleanup is best-effort and never alters the status.
    """
    # 1. Fail-closed guards — delete NOTHING in any refuse case.
    from product.demo_identity import is_demo_csm_id
    if is_demo_csm_id(clerk_user_id):
        return {"status": "not_purgeable", "reason": "sarah_c", "clerk_user_id": clerk_user_id}
    if SessionLocal is None:
        return {"status": "not_purgeable", "reason": "no_db", "clerk_user_id": clerk_user_id}

    session = SessionLocal()
    try:
        # 2. Resolve the root row by the literal clerk_user_id.
        cust = session.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        if cust is None:
            return {
                "status": "not_purgeable",
                "reason": "no_customer",
                "clerk_user_id": clerk_user_id,
            }
        internal_uuid = cust.id  # capture for the response before the delete

        # 3. Delete dependents then the root row, all in this transaction.
        rows_deleted: dict = {}
        for key, model, column in _PURGE_TABLE_SPECS:
            rows_deleted[key] = _bulk_delete(session, model, column, clerk_user_id)
        rows_deleted["customers"] = _bulk_delete(
            session, Customer, "clerk_user_id", clerk_user_id
        )

        # 4. Commit once. Any delete above raising falls to except → rollback.
        session.commit()
    except Exception as exc:
        session.rollback()
        return {"status": "failed", "error": str(exc), "clerk_user_id": clerk_user_id}
    finally:
        session.close()

    # 5. Success — one structured INFO line with the per-table counts.
    logger.info(
        "event=user_data_purged clerk_user_id=%s customer_id=%s rows_deleted=%s",
        clerk_user_id, internal_uuid, rows_deleted,
    )

    # 6. Best-effort on-disk cleanup AFTER commit. Never fails the purge.
    _purge_on_disk(clerk_user_id)

    return {
        "status": "purged",
        "clerk_user_id": clerk_user_id,
        "customer_id": internal_uuid,
        "rows_deleted": rows_deleted,
    }


# ---------------------------------------------------------------------------
# Per-CRM disconnect — surgically removes one CRM's stored token (and the
# CRM-derived snapshot, when that snapshot was built from this CRM). Far
# narrower than purge_user_data: it touches exactly two tables and never the
# customers root row. Fulfills the privacy-policy promise that a user can
# disconnect HubSpot or Gainsight at any time.
# ---------------------------------------------------------------------------

_CRM_PROVIDERS = ("hubspot", "gainsight")


def disconnect_crm(clerk_user_id: str, provider: str) -> dict:
    """Disconnect one CRM for a user: delete its token row and, per the
    crm-match rule, its CRM-derived context snapshot — atomically.

    Token storage commits on its OWN session (see token_store.delete_token),
    so it cannot be reused here: the token delete and the snapshot delete must
    share ONE transaction so a snapshot-delete failure rolls back the token
    delete too. Nothing partial ever persists.

    Snapshot disposition (the "crm-match" rule):
      * Delete the user_context_snapshots row IFF that snapshot was built from
        `provider` (its stored crm_type == provider).
      * If crm_type cannot be determined (no row, context_json empty/"{}", or
        crm_type absent/unparseable), delete the snapshot only when NO CRM
        token remains after this disconnect — i.e. the OTHER CRM has no
        connections row; otherwise keep it.
      * If crm_type is the OTHER CRM, keep the snapshot.

    Deletes ONLY the connections row and (conditionally) the
    user_context_snapshots row. Never touches digest_logs, proactive_alerts,
    edit_deltas, user_preferences, signal_cache, engagement_sends, oauth_states,
    the customers root row, or the on-disk accounts scope file (CRM-agnostic,
    still needed by the remaining/reconnected CRM).

    Returns {"token_deleted": bool, "snapshot_deleted": bool,
             "snapshot_crm_type": str|None}. Never raises out except ValueError
    for an invalid provider (defense in depth — the endpoint allowlists too;
    "google" must never reach here). Any DB error rolls the transaction back
    and surfaces as token_deleted/snapshot_deleted both False.
    """
    if provider not in _CRM_PROVIDERS:
        raise ValueError(f"disconnect_crm: provider must be one of {_CRM_PROVIDERS}, got {provider!r}")
    other = "gainsight" if provider == "hubspot" else "hubspot"

    if SessionLocal is None:
        return {"token_deleted": False, "snapshot_deleted": False, "snapshot_crm_type": None}

    session = SessionLocal()
    token_deleted = False
    snapshot_deleted = False
    snapshot_crm_type: str | None = None
    reason = ""
    try:
        # a. Read the snapshot row and its stored crm_type.
        snap = session.query(UserContextSnapshot).filter_by(csm_id=clerk_user_id).first()
        snapshot_exists = snap is not None
        if snapshot_exists and snap.context_json:
            try:
                parsed = json.loads(snap.context_json)
                if isinstance(parsed, dict):
                    ct = parsed.get("crm_type")
                    snapshot_crm_type = ct if isinstance(ct, str) and ct else None
            except (ValueError, TypeError):
                snapshot_crm_type = None

        # b. Decide snapshot disposition per the crm-match rule.
        if not snapshot_exists:
            delete_snapshot = False
            reason = "no_snapshot"
        elif snapshot_crm_type == provider:
            delete_snapshot = True
            reason = "crm_match"
        elif snapshot_crm_type is None:
            # Undeterminable: delete only if the OTHER CRM also has no token.
            other_conn = session.query(Connection).filter_by(
                customer_id=clerk_user_id, provider=other
            ).first()
            if other_conn is None:
                delete_snapshot = True
                reason = "undeterminable_no_other_crm"
            else:
                delete_snapshot = False
                reason = "undeterminable_other_crm_present"
        else:
            # Snapshot belongs to the OTHER CRM — keep it.
            delete_snapshot = False
            reason = "snapshot_other_crm"

        # c. Delete the connections row for this provider, if present.
        conn = session.query(Connection).filter_by(
            customer_id=clerk_user_id, provider=provider
        ).first()
        if conn is not None:
            session.delete(conn)
            token_deleted = True

        # d. Conditionally delete the snapshot row.
        if delete_snapshot:
            session.delete(snap)
            snapshot_deleted = True

        # e. Commit once.
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning(
            "event=crm_disconnect_failed csm_id=%s provider=%s error=%s",
            clerk_user_id, provider, exc,
        )
        return {"token_deleted": False, "snapshot_deleted": False, "snapshot_crm_type": None}
    finally:
        session.close()

    logger.info(
        "event=crm_disconnect csm_id=%s provider=%s token_deleted=%s "
        "snapshot_deleted=%s snapshot_crm_type=%s reason=%s",
        clerk_user_id, provider, token_deleted, snapshot_deleted,
        snapshot_crm_type, reason,
    )
    return {
        "token_deleted": token_deleted,
        "snapshot_deleted": snapshot_deleted,
        "snapshot_crm_type": snapshot_crm_type,
    }


# ---------------------------------------------------------------------------
# Recurring cancel-purge sweep — the first real caller of purge_user_data.
# ---------------------------------------------------------------------------

_PURGE_SWEEP_LOCK_KEY = 728142          # stable bigint for the pg advisory lock
_PURGE_SWEEP_CUTOFF_DAYS = 29           # 29, not 30: one-day margin so the 30-day policy ceiling is provably never crossed
_PURGE_SWEEP_MAX_PER_RUN = 10           # runaway rail: more than this eligible in one run => purge nothing, alert


def _try_sweep_lock(session) -> bool:
    """Acquire the cross-instance advisory lock for the cancel sweep.
    True if acquired. On SQLite (tests) pg_try_advisory_lock does not
    exist, so we treat that as single-runner and proceed. Isolated as a
    seam so a test can force the lock-unavailable path."""
    try:
        row = session.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _PURGE_SWEEP_LOCK_KEY},
        ).first()
        return bool(row[0]) if row else False
    except Exception:
        return True


def _release_sweep_lock(session) -> None:
    try:
        session.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": _PURGE_SWEEP_LOCK_KEY},
        )
    except Exception:
        pass


def purge_expired_cancellations(
    cutoff_days: int = _PURGE_SWEEP_CUTOFF_DAYS,
    max_per_run: int = _PURGE_SWEEP_MAX_PER_RUN,
) -> dict:
    """Hard-purge users canceled more than cutoff_days ago.

    Gathers eligible clerk_user_ids READ-ONLY (subscription_status
    'canceled', NOT is_comp_account, canceled_at set and older than the
    cutoff), enforces a per-run cap as a runaway rail, then calls
    purge_user_data per id. Each purge is its own transaction, so one
    bad id cannot poison the rest. Cross-instance safe via a Postgres
    advisory lock held for the whole run. Comp accounts (the reviewer-test
    canary, design partners, advisors) are excluded by the gather filter,
    NOT merely by purge_user_data's guards, since the primitive only
    refuses sarah_c. Never raises for per-user failures. Returns a status
    dict: no_db | locked | none_eligible | cap_exceeded | purged.
    """
    if SessionLocal is None:
        return {"status": "no_db", "eligible_count": 0, "purged": 0, "failed": 0}

    session = SessionLocal()
    locked = False
    try:
        if not _try_sweep_lock(session):
            return {"status": "locked", "eligible_count": 0, "purged": 0, "failed": 0}
        locked = True

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=cutoff_days)

        # Coarse SQL filter; apply the cutoff in Python via as_utc so the
        # comparison is tz-safe on both SQLite (naive, tests) and Postgres
        # (aware, prod), mirroring canceled_users_in_privacy_breach.
        candidates = session.query(Customer).filter(
            Customer.subscription_status == "canceled",
            Customer.is_comp_account.is_(False),
            Customer.canceled_at.isnot(None),
        ).all()
        eligible_ids = [
            c.clerk_user_id for c in candidates
            if c.clerk_user_id and as_utc(c.canceled_at) < cutoff
        ]
        eligible_count = len(eligible_ids)

        if eligible_count == 0:
            logger.info(
                "event=purge_sweep_complete eligible=0 purged=0 failed=0 cutoff_days=%d",
                cutoff_days,
            )
            return {"status": "none_eligible", "eligible_count": 0, "purged": 0, "failed": 0}

        if eligible_count > max_per_run:
            logger.error(
                "event=purge_sweep_cap_exceeded eligible=%d cap=%d cutoff_days=%d PURGING NOTHING",
                eligible_count, max_per_run, cutoff_days,
            )
            return {
                "status": "cap_exceeded",
                "eligible_count": eligible_count,
                "purged": 0, "failed": 0, "cap": max_per_run,
            }

        purged = 0
        failed = 0
        for cid in eligible_ids:
            try:
                result = purge_user_data(cid)
                if result.get("status") == "purged":
                    purged += 1
                else:
                    failed += 1
                    logger.warning(
                        "event=purge_sweep_user_not_purged clerk_user_id=%s result_status=%s",
                        cid, result.get("status"),
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "event=purge_sweep_user_failed clerk_user_id=%s error=%s",
                    cid, exc,
                )

        logger.info(
            "event=purge_sweep_complete eligible=%d purged=%d failed=%d cutoff_days=%d",
            eligible_count, purged, failed, cutoff_days,
        )
        return {"status": "purged", "eligible_count": eligible_count, "purged": purged, "failed": failed}
    finally:
        if locked:
            _release_sweep_lock(session)
        session.close()
