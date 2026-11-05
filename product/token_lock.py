"""
product/token_lock.py

Per-(csm_id, provider) lock for OAuth token refresh.

The bug fixed here (audit Issue #6, Day 3 sprint): N concurrent requests for
the same CSM that all observe an expired token would each independently call
the OAuth provider's refresh endpoint with the same refresh_token. Some
providers invalidate prior refresh_tokens on use; under contention this
produces transient 401s and burned refresh_token grants.

Design:
- Lock primitive is threading.Lock so the same lock works for sync callers
  (auth_utils.load_google_credentials, connector __init__) and async callers
  (connector.batch(), 401-retry paths).
- Async callers acquire via asyncio.to_thread so the event loop isn't
  blocked while waiting on contention.
- Lock granularity is (csm_id, provider) — one user's HubSpot refresh does
  not block another user's, nor does it block the same user's Google
  refresh.
- Locks are created lazily and held for the process lifetime. The dict
  grows by one entry per (csm_id, provider) pair ever observed; for the
  expected ~hundreds-of-CSMs cardinality this is a non-concern. If
  cardinality grows much larger, replace _locks with an LRU cache.

Limitations:
- This lock is process-local. Web (uvicorn) and worker (arq) are separate
  processes; a refresh in one doesn't serialize with one in the other.
  Cross-process races are rare in practice (worker jobs are queued, not
  real-time) and addressed by a follow-up Tech Debt item: pg_advisory_lock
  or Redis-backed lock.

Usage — sync:
    from product.token_lock import token_lock
    with token_lock(csm_id, "google"):
        # double-check token freshness here, then refresh if still needed

Usage — async:
    from product.token_lock import token_lock_async
    async with token_lock_async(csm_id, "hubspot"):
        # same pattern
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager

# Per-(csm_id, provider) locks. Lazy-created; persist for process lifetime.
_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_lock(csm_id: str, provider: str) -> threading.Lock:
    key = (csm_id, provider)
    # Fast path — no contention on the guard once the lock exists
    lock = _locks.get(key)
    if lock is not None:
        return lock
    # Slow path — create under guard
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


@contextmanager
def token_lock(csm_id: str, provider: str):
    """Sync context manager for the per-(csm_id, provider) refresh lock."""
    lock = _get_lock(csm_id, provider)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


@asynccontextmanager
async def token_lock_async(csm_id: str, provider: str):
    """Async context manager — acquires via asyncio.to_thread so the event
    loop continues to serve other requests while this one waits on contention.
    """
    lock = _get_lock(csm_id, provider)
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        lock.release()
