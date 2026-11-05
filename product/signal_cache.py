"""
product/signal_cache.py

General-purpose signal cache. Downstream consumers (/chat, analytics,
inward-loop) read recent signals without re-running extractors.

Scope today: gmail signals written by _run_digest_for_user and
_run_absence_detection. Calendar / CRM signals can adopt the same
table later — schema is extractor-agnostic.

Freshness is a read-side filter (max_age_minutes). Stale rows are
left in place by design; they age out of reads naturally and do not
require a sweeper.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("cs-agent.signal-cache")


def derive_gmail_signal_id(signal: dict) -> str:
    """{thread_id}:{contact}:{account} — stable across repeated extractions.
    Two runs with the same source thread + contact + account produce the
    same signal_id, which drives the upsert path.
    """
    return f"{signal['thread_id']}:{signal['contact']}:{signal['account']}"


def write_signals(csm_id: str, signal_type: str, signals: list[dict]) -> int:
    """Upsert a batch of signals into the cache. Returns rows written.

    Empty list is a true no-op — returns 0 with zero DB round-trips.
    This matters because callers invoke write_signals() immediately
    after the extractor's try/except, so the failure path legitimately
    passes signals=[].

    On conflict (same csm_id + signal_type + signal_id), updates
    signal_payload and refreshed_at. Last-writer-wins when
    _run_digest_for_user and _run_absence_detection fire concurrently
    for the same csm_id; accepted tradeoff (A.1.5 Commit 2 scope).

    Known tradeoff — orphan rows: if account attribution for a contact
    changes between runs (e.g. user reassigns contact to a different
    account), the old signal_id persists until manual cleanup. It does
    not affect reads — freshness window excludes it after 60 min.
    Not solving in this commit.

    Never raises. Logs warnings on failure and returns 0.
    """
    if not signals:
        return 0

    from product.database import SessionLocal, SignalCache
    # Dialect branch: pg_insert for Postgres (prod), sqlite_insert for
    # SQLite (tests). The branch lives in production code rather than
    # being hidden behind a test adapter because write_signals is the
    # single source of truth for the upsert semantics — duplicating
    # the on_conflict_do_update surface in a test-only shim would let
    # the two copies drift. Both dialects accept the same
    # on_conflict_do_update(index_elements=..., set_=...) API.
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    if not SessionLocal:
        logger.warning("[signal-cache] DB not configured, skipping write for %s", csm_id)
        return 0

    now = datetime.now(timezone.utc)
    rows = []
    for sig in signals:
        try:
            sid = derive_gmail_signal_id(sig)
        except KeyError as exc:
            logger.warning("[signal-cache] %s: signal missing key %s — skipping", csm_id, exc)
            continue
        rows.append({
            "csm_id": csm_id,
            "signal_type": signal_type,
            "signal_id": sid,
            "signal_payload": sig,      # full 8-key dict verbatim
            "refreshed_at": now,
        })

    if not rows:
        return 0

    session = SessionLocal()
    try:
        dialect = session.bind.dialect.name
        insert_fn = sqlite_insert if dialect == "sqlite" else pg_insert
        stmt = insert_fn(SignalCache).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["csm_id", "signal_type", "signal_id"],
            set_={
                "signal_payload": stmt.excluded.signal_payload,
                "refreshed_at":   stmt.excluded.refreshed_at,
            },
        )
        session.execute(stmt)
        session.commit()
        return len(rows)
    except Exception as exc:
        logger.warning("[signal-cache] write failed for %s/%s: %s", csm_id, signal_type, exc)
        session.rollback()
        return 0
    finally:
        session.close()


def write_gmail_signals(csm_id: str, signals: list[dict]) -> dict[str, int]:
    """Group a mixed list of gmail signals by signal_type and upsert each
    group. Returns {signal_type: rows_written}.

    Signals missing a signal_type key are logged and skipped — we do not
    bucket them into a catch-all type because that would silently mask
    extractor bugs.

    Collapses the grouping-loop boilerplate that would otherwise live
    at every call site.
    """
    if not signals:
        return {}

    by_type: dict[str, list[dict]] = {}
    for s in signals:
        stype = s.get("signal_type")
        if not stype:
            logger.warning(
                "[signal-cache] %s: signal missing signal_type — skipping: %r",
                csm_id, s,
            )
            continue
        by_type.setdefault(stype, []).append(s)

    return {stype: write_signals(csm_id, stype, sigs) for stype, sigs in by_type.items()}


def read_signals(
    csm_id: str,
    signal_type: str | None = None,
    max_age_minutes: int = 60,
) -> list[dict]:
    """Read fresh signal payloads for a user. Returns payloads only,
    not full rows.

    Rows with refreshed_at older than max_age_minutes are excluded.
    When no rows match (never written, all stale, or DB not configured),
    returns [] — never None.
    """
    from product.database import SessionLocal, SignalCache, as_utc

    if not SessionLocal:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    session = SessionLocal()
    try:
        q = session.query(SignalCache).filter(SignalCache.csm_id == csm_id)
        if signal_type is not None:
            q = q.filter(SignalCache.signal_type == signal_type)
        rows = q.all()
        # Freshness filter runs in Python, not SQL, because SQLite (tests)
        # returns naive datetimes and Postgres (prod) returns aware
        # datetimes — a SQL-side `WHERE refreshed_at >= :cutoff` would
        # raise a TypeError on one backend or the other depending on
        # how psycopg2/SQLAlchemy coerce the bind param. as_utc() hides
        # that divergence.
        #
        # Scale note: O(rows_for_user) per read. At ~10K rows/user this
        # becomes the dominant cost and we should move the filter to SQL
        # (timezone=True columns + backend-specific cutoff coercion).
        # Typical user today has <500 cached signals — fine in Python.
        return [
            r.signal_payload for r in rows
            if as_utc(r.refreshed_at) is not None and as_utc(r.refreshed_at) >= cutoff
        ]
    except Exception as exc:
        logger.warning("[signal-cache] read failed for %s: %s", csm_id, exc)
        return []
    finally:
        session.close()
