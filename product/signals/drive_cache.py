"""
product/signals/drive_cache.py

Process-local LRU cache for Google Docs transcript snippets.

Cache key: (file_id, modified_time, CACHE_VERSION). All strings/ints.
Cache value: snippet string up to 2000 chars.

modified_time in the key gives natural invalidation: a transcript that
gets edited yields a new modifiedTime from Drive, which is a cache miss.

CACHE_VERSION invalidates the entire cache when the snippet-extraction
contract changes — e.g. v1 (broken) returned tab-0 Notes content for
multi-tab Docs; v2 (current) returns the verbatim Transcript-tab content
via documents.get(includeTabsContent=True). Bumping the version means
every v1 entry becomes unreachable on deploy and garbage-collects via
LRU eviction. No manual flush needed.

Module-level state. file_id is globally unique across CSMs, so no
per-CSM partitioning is needed; the cache is shared across all
in-flight /chat requests in the process.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger("cs-agent.drive_cache")

CACHE_VERSION = 2  # v1 = Notes content (broken), v2 = Transcript-tab content

_MAXSIZE = 500
_cache: "OrderedDict[tuple[str, str, int], str]" = OrderedDict()
_lock = threading.Lock()
_hits = 0
_misses = 0


def get_snippet(file_id: str, modified_time: str) -> Optional[str]:
    """Return the cached snippet for (file_id, modified_time), or None on miss."""
    global _hits, _misses
    key = (file_id, modified_time, CACHE_VERSION)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            _hits += 1
            value = _cache[key]
            logger.debug("drive_cache HIT file_id=%s hits=%d misses=%d", file_id, _hits, _misses)
            return value
        _misses += 1
        logger.debug("drive_cache MISS file_id=%s hits=%d misses=%d", file_id, _hits, _misses)
        return None


def set_snippet(file_id: str, modified_time: str, snippet: str) -> None:
    """Store snippet in the cache, evicting the oldest entry if at capacity."""
    key = (file_id, modified_time, CACHE_VERSION)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            _cache[key] = snippet
            return
        _cache[key] = snippet
        if len(_cache) > _MAXSIZE:
            evicted_key, _ = _cache.popitem(last=False)
            logger.debug("drive_cache EVICT file_id=%s", evicted_key[0])


def stats() -> dict:
    """Return current cache stats. Useful for tests and observability."""
    with _lock:
        return {"size": len(_cache), "hits": _hits, "misses": _misses, "maxsize": _MAXSIZE}


def clear() -> None:
    """Empty the cache and reset counters. Test-only helper."""
    global _hits, _misses
    with _lock:
        _cache.clear()
        _hits = 0
        _misses = 0
