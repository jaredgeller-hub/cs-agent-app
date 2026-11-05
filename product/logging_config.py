"""
product/logging_config.py

Centralized structured logging configuration for CS Agent.
JSON-formatted log lines for Render log aggregation.
Request correlation IDs for tracing across log lines.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# Context variable for per-request correlation ID
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
csm_id_var: ContextVar[str] = ContextVar("csm_id", default="-")


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Add correlation context if available
        _req_id = request_id_var.get("-")
        _csm_id = csm_id_var.get("-")
        if _req_id != "-":
            log_entry["request_id"] = _req_id
        if _csm_id != "-":
            log_entry["csm_id"] = _csm_id
        # Add exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Set up structured JSON logging on the root logger.

    Call once at app startup (main.py and worker.sh).
    All existing loggers (cs-agent.hubspot_crm, cs-agent.queue, etc.)
    automatically inherit this configuration.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any existing handlers (avoid duplicate output)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)
