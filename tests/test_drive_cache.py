"""
tests/test_drive_cache.py

Unit tests for product/signals/drive_cache.py.

Covers:
  1. get/set roundtrip and miss-returns-None
  2. LRU eviction behavior at maxsize
  3. Different modified_time for the same file_id is a different key
  4. Concurrent get/set is thread-safe under contention

Standalone-runnable:
    python3 tests/test_drive_cache.py
"""

import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.signals import drive_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    drive_cache.clear()
    yield
    drive_cache.clear()


def test_miss_returns_none():
    assert drive_cache.get_snippet("file_unknown", "2026-05-04T00:00:00Z") is None
    stats = drive_cache.stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 0
    assert stats["size"] == 0


def test_set_then_get_roundtrip():
    drive_cache.set_snippet("file_a", "2026-05-04T00:00:00Z", "hello world")
    assert drive_cache.get_snippet("file_a", "2026-05-04T00:00:00Z") == "hello world"
    stats = drive_cache.stats()
    assert stats["hits"] == 1
    assert stats["size"] == 1


def test_different_modified_time_is_different_entry():
    drive_cache.set_snippet("file_a", "2026-05-04T00:00:00Z", "v1 snippet")
    drive_cache.set_snippet("file_a", "2026-05-04T01:00:00Z", "v2 snippet")

    assert drive_cache.get_snippet("file_a", "2026-05-04T00:00:00Z") == "v1 snippet"
    assert drive_cache.get_snippet("file_a", "2026-05-04T01:00:00Z") == "v2 snippet"
    assert drive_cache.stats()["size"] == 2


def test_lru_eviction_at_maxsize():
    """Inserting maxsize+1 entries evicts the oldest."""
    maxsize = drive_cache.stats()["maxsize"]

    for i in range(maxsize):
        drive_cache.set_snippet(f"file_{i}", "t", f"snippet_{i}")

    assert drive_cache.stats()["size"] == maxsize
    assert drive_cache.get_snippet("file_0", "t") == "snippet_0"

    drive_cache.clear()
    for i in range(maxsize):
        drive_cache.set_snippet(f"file_{i}", "t", f"snippet_{i}")

    drive_cache.set_snippet("file_overflow", "t", "overflow")
    assert drive_cache.stats()["size"] == maxsize
    assert drive_cache.get_snippet("file_0", "t") is None
    assert drive_cache.get_snippet("file_overflow", "t") == "overflow"


def test_lru_get_promotes_recency():
    """Reading file_0 promotes it; subsequent eviction targets file_1."""
    maxsize = drive_cache.stats()["maxsize"]

    for i in range(maxsize):
        drive_cache.set_snippet(f"file_{i}", "t", f"snippet_{i}")

    assert drive_cache.get_snippet("file_0", "t") == "snippet_0"
    drive_cache.set_snippet("file_overflow", "t", "overflow")

    assert drive_cache.get_snippet("file_0", "t") == "snippet_0"
    assert drive_cache.get_snippet("file_1", "t") is None


def test_set_existing_key_updates_value():
    drive_cache.set_snippet("file_a", "t", "v1")
    drive_cache.set_snippet("file_a", "t", "v2")
    assert drive_cache.get_snippet("file_a", "t") == "v2"
    assert drive_cache.stats()["size"] == 1


def test_concurrent_get_set_is_thread_safe():
    """Hammer the same key from N threads. No exceptions, final state consistent."""
    file_id = "file_concurrent"
    modified_time = "2026-05-04T00:00:00Z"
    iterations = 200
    n_threads = 16
    errors: list[BaseException] = []

    def worker(thread_idx: int):
        try:
            for i in range(iterations):
                drive_cache.set_snippet(file_id, modified_time, f"t{thread_idx}-i{i}")
                drive_cache.get_snippet(file_id, modified_time)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for thr in threads:
        thr.start()
    for thr in threads:
        thr.join()

    assert errors == []
    final = drive_cache.get_snippet(file_id, modified_time)
    assert isinstance(final, str)
    assert final.startswith("t")
    assert drive_cache.stats()["size"] == 1


def test_concurrent_distinct_keys_no_corruption():
    """Each thread writes a unique key. All entries should be retrievable up to maxsize."""
    n_threads = 20
    per_thread = 10
    errors: list[BaseException] = []

    def worker(thread_idx: int):
        try:
            for i in range(per_thread):
                drive_cache.set_snippet(
                    f"file_t{thread_idx}_i{i}", "t", f"snippet_t{thread_idx}_i{i}"
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for thr in threads:
        thr.start()
    for thr in threads:
        thr.join()

    assert errors == []
    total_written = n_threads * per_thread
    maxsize = drive_cache.stats()["maxsize"]
    expected_size = min(total_written, maxsize)
    assert drive_cache.stats()["size"] == expected_size


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()
