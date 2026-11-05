"""
tests/test_token_refresh_locking.py

Verifies product.token_lock serializes per-(csm_id, provider) token refreshes
and that the double-check pattern (re-read inside the lock) prevents redundant
refresh calls when another caller just refreshed.

The bug fixed (audit Issue #6, Day 3): N concurrent requests for the same CSM
that all observed an expired token would each independently call the OAuth
provider's refresh endpoint with the same refresh_token. Some providers
invalidate prior refresh_tokens on use; this produced transient 401s and
burned grants under contention.

The fix is a process-local threading.Lock keyed on (csm_id, provider), with
sync and async context-manager wrappers. Cross-process races (web ↔ worker)
are a separate Tech Debt item — this test covers the intra-process case.

Test cases:
  1. 10 parallel callers, same (csm_id, provider) → exactly 1 refresh call
  2. Different (csm_id, provider) keys do NOT block each other
  3. Lock is released after an exception
  4. Double-check fires correctly: second caller arriving AFTER first
     released sees fresh token and SKIPS the refresh

Mixes async (asyncio.gather) and sync (ThreadPoolExecutor) to exercise both
sides of the threading.Lock primitive in a single test process.

Standalone-runnable today; structured for Day 3 CI promotion:
    python3 tests/test_token_refresh_locking.py
"""

import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from product.token_lock import token_lock, token_lock_async, _locks


def _reset_locks():
    """Drop the module-level lock dict between tests for isolation."""
    _locks.clear()


# ── Fake token store with a counter on refresh ────────────────────────────────

class FakeTokenStore:
    """Mimics the (csm_id, provider) → token_data store with refresh counter."""

    def __init__(self, initial_token: str | None = "stale_token"):
        self._token = initial_token
        self._lock = threading.Lock()
        self.refresh_count = 0
        self.refresh_delay = 0.05  # seconds — long enough to expose races

    def load(self):
        with self._lock:
            return self._token

    def refresh(self):
        """Simulates the OAuth provider's refresh endpoint. Slow + counted."""
        time.sleep(self.refresh_delay)
        self.refresh_count += 1
        new_token = f"fresh_token_{self.refresh_count}"
        with self._lock:
            self._token = new_token
        return new_token


# ── Helpers that simulate the production refresh pattern ─────────────────────

def sync_refresh_under_lock(store: FakeTokenStore, csm_id: str, provider: str,
                             needs_refresh) -> str:
    """Mirrors the sync sites (auth_utils, connector __init__).

    needs_refresh: callable(token) -> bool — caller-supplied threshold predicate.
    """
    token = store.load()
    if needs_refresh(token):
        with token_lock(csm_id, provider):
            # Double-check
            token = store.load()
            if needs_refresh(token):
                token = store.refresh()
    return token


async def async_refresh_under_lock(store: FakeTokenStore, csm_id: str,
                                     provider: str, needs_refresh) -> str:
    """Mirrors the async sites (connector batch + 401 retry).

    The fake refresh uses time.sleep() to simulate blocking I/O. We offload
    it to a thread so different-key locks can actually run in parallel —
    matches the behavior we want from properly-async refresh paths even
    though production currently calls refresh_*_token sync inside the lock.
    """
    token = store.load()
    if needs_refresh(token):
        async with token_lock_async(csm_id, provider):
            token = store.load()
            if needs_refresh(token):
                token = await asyncio.to_thread(store.refresh)
    return token


# ── Test 1: 10 parallel callers, same key → exactly 1 refresh ────────────────

def test_ten_parallel_callers_one_refresh():
    _reset_locks()
    store = FakeTokenStore(initial_token="stale_token")
    needs = lambda t: t == "stale_token"

    async def runner():
        # 5 async tasks + 5 threadpool tasks, all racing on the same key
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=5) as pool:
            sync_futures = [
                loop.run_in_executor(
                    pool, sync_refresh_under_lock, store, "user_X", "google", needs
                )
                for _ in range(5)
            ]
            async_futures = [
                async_refresh_under_lock(store, "user_X", "google", needs)
                for _ in range(5)
            ]
            results = await asyncio.gather(*sync_futures, *async_futures)
        return results

    results = asyncio.run(runner())

    assert store.refresh_count == 1, (
        f"expected exactly 1 refresh, got {store.refresh_count}"
    )
    # All 10 callers got the same fresh token
    assert all(r == "fresh_token_1" for r in results), (
        f"callers got mismatched tokens: {set(results)}"
    )
    print(f"✓ 10 parallel callers → exactly 1 refresh (got: {set(results)})")


# ── Test 2: different keys do NOT serialize ───────────────────────────────────

def test_different_keys_do_not_block():
    _reset_locks()
    store_a = FakeTokenStore(initial_token="stale_a")
    store_b = FakeTokenStore(initial_token="stale_b")
    store_c = FakeTokenStore(initial_token="stale_c")
    # Make refresh slow enough that serialized execution would be obvious
    for s in (store_a, store_b, store_c):
        s.refresh_delay = 0.30

    needs_a = lambda t: t == "stale_a"
    needs_b = lambda t: t == "stale_b"
    needs_c = lambda t: t == "stale_c"

    async def runner():
        start = time.time()
        await asyncio.gather(
            async_refresh_under_lock(store_a, "user_A", "google", needs_a),
            async_refresh_under_lock(store_b, "user_B", "google", needs_b),
            async_refresh_under_lock(store_c, "user_A", "hubspot", needs_c),
        )
        return time.time() - start

    elapsed = asyncio.run(runner())

    # Three independent locks → all three refresh in parallel.
    # Serialized would take ~0.90s; parallel takes ~0.30s. Allow generous margin.
    assert elapsed < 0.60, (
        f"different-key refreshes serialized (took {elapsed:.3f}s, "
        f"expected <0.60s for parallel ~0.30s baseline)"
    )
    assert store_a.refresh_count == 1
    assert store_b.refresh_count == 1
    assert store_c.refresh_count == 1
    print(f"✓ 3 different keys ran in parallel (elapsed {elapsed:.3f}s; serialized would be ~0.90s)")


# ── Test 3: lock released after exception ─────────────────────────────────────

def test_lock_released_after_exception():
    _reset_locks()
    store = FakeTokenStore(initial_token="stale_token")

    # First caller raises inside the lock
    needs = lambda t: t == "stale_token"
    raised = False
    try:
        with token_lock("user_E", "google"):
            store.refresh()  # advances state to fresh_token_1
            raise RuntimeError("simulated refresh failure")
    except RuntimeError:
        raised = True
    assert raised, "expected RuntimeError to propagate"

    # Second caller — must be able to acquire (proves lock was released)
    acquired = False
    with token_lock("user_E", "google"):
        acquired = True
    assert acquired, "lock was not released after exception"
    print("✓ lock released after exception (second caller acquired)")


# ── Test 4: double-check fires correctly — 2 staggered async calls ────────────

def test_double_check_skips_redundant_refresh():
    """Caller 1 acquires lock, refreshes, releases.
    Caller 2 arrives AFTER release. Inside its lock acquire, it re-reads the
    token, sees it's already fresh, and SKIPS the refresh.
    """
    _reset_locks()
    store = FakeTokenStore(initial_token="stale_token")
    needs = lambda t: t == "stale_token"

    async def caller_1():
        # Run sync helper in executor for variety; both should see counter=1
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, sync_refresh_under_lock, store, "user_DC", "google", needs
        )

    async def caller_2():
        # Wait until caller_1 has definitely finished
        await asyncio.sleep(0.10)
        return await async_refresh_under_lock(store, "user_DC", "google", needs)

    async def runner():
        return await asyncio.gather(caller_1(), caller_2())

    r1, r2 = asyncio.run(runner())

    assert store.refresh_count == 1, (
        f"second caller refreshed redundantly — counter={store.refresh_count} "
        f"(double-check pattern broken)"
    )
    assert r1 == "fresh_token_1"
    assert r2 == "fresh_token_1", f"caller 2 got {r2!r}, expected fresh_token_1"
    print(f"✓ double-check: caller 2 saw fresh token, skipped refresh (counter=1)")


# ── Bonus: same-key reuse across many sequential callers (locks persist) ─────

def test_lock_dict_reuses_existing_lock():
    _reset_locks()
    from product.token_lock import _get_lock
    lock_a1 = _get_lock("user_R", "google")
    lock_a2 = _get_lock("user_R", "google")
    lock_b = _get_lock("user_R", "hubspot")
    assert lock_a1 is lock_a2, "same key returned different lock instances"
    assert lock_a1 is not lock_b, "different keys returned same lock"
    print("✓ lock dict reuses lock instance per key")


def main():
    tests = [
        test_ten_parallel_callers_one_refresh,
        test_different_keys_do_not_block,
        test_lock_released_after_exception,
        test_double_check_skips_redundant_refresh,
        test_lock_dict_reuses_existing_lock,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
            failures.append(t.__name__)
        except Exception as e:
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
            failures.append(t.__name__)
    print()
    if failures:
        print(f"FAILED: {len(failures)} of {len(tests)}")
        for n in failures:
            print(f"  - {n}")
        sys.exit(1)
    print(f"PASSED: {len(tests)} of {len(tests)}")


if __name__ == "__main__":
    main()
