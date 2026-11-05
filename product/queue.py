"""
product/queue.py

arq Redis queue helper. Provides enqueue() for submitting background jobs.
Falls back gracefully when Redis is not available.
"""

from __future__ import annotations

import logging
import os
import time as _time

logger = logging.getLogger("cs-agent.queue")

_pool = None
_redis_available: bool | None = None  # None = not checked yet
_redis_last_check: float = 0.0


async def _get_pool():
    """Return or create the ArqRedis connection pool."""
    global _pool
    if _pool is not None:
        return _pool
    try:
        from arq.connections import create_pool
        from product.worker import _parse_redis_settings
        _pool = await create_pool(_parse_redis_settings())
        return _pool
    except Exception as exc:
        logger.warning("[queue] Could not create Redis pool: %s", exc)
        return None


async def check_redis() -> bool:
    """Check if Redis is reachable. Rechecks every 60s if last check was False."""
    global _redis_available, _redis_last_check
    if _redis_available is True:
        return True
    if _redis_available is False and (_time.time() - _redis_last_check) < 60:
        return False
    # Recheck
    _redis_last_check = _time.time()
    try:
        pool = await _get_pool()
        if pool is None:
            _redis_available = False
        else:
            await pool.ping()
            _redis_available = True
    except Exception:
        _redis_available = False
    return _redis_available


def is_redis_available() -> bool:
    """Return cached Redis availability (synchronous). Returns False if not yet checked."""
    return _redis_available is True


async def enqueue(func_name: str, *args, _dedup_key: str | None = None, **kwargs):
    """Enqueue an arq job. Returns the Job object, or None if Redis is unavailable.

    If _dedup_key is provided, skips enqueue if a job with that key was
    enqueued within the last 120 seconds. Uses a Redis key with TTL
    so stale guards auto-expire (unlike arq's built-in _job_id).
    """
    pool = await _get_pool()
    if pool is None:
        return None

    # Dedup guard: check if this job was recently enqueued
    if _dedup_key:
        try:
            _guard_key = f"dedup:{_dedup_key}"
            _exists = await pool.exists(_guard_key)
            if _exists:
                logger.info("[queue] Dedup: skipping %s (key=%s, already queued)", func_name, _dedup_key)
                return None
            # Set guard with 120s TTL
            await pool.set(_guard_key, "1", ex=120)
        except Exception as _de:
            # If Redis dedup check fails, enqueue anyway
            logger.debug("[queue] Dedup check failed (non-fatal): %s", _de)

    try:
        job = await pool.enqueue_job(func_name, *args, **kwargs)
        logger.info("enqueue result for %s: %s", func_name, job)
        return job
    except Exception as exc:
        logger.warning("[queue] Failed to enqueue %s: %s", func_name, exc)
        return None
