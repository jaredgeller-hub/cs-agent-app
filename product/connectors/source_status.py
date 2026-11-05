"""SourceStatus enum threaded through the MCP read path.

Distinguishes failure (network, auth, rate limit, circuit breaker, upstream
error, timeout) from legitimate empty results so consumers can degrade
gracefully instead of conflating outages with "no data".

v1 deliberately collapses every failure mode to FAILED. A separate
UNAUTHENTICATED value is deferred to Sprint 6, where it pairs with a
reconnect UX flow.
"""

from __future__ import annotations

from enum import Enum


class SourceStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"
