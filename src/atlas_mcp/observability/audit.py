"""Component 11 (audit leg) — Structured audit log.

Audit logs are different from application logs. They are the tamper-aware,
regulator-readable paper trail of "who called what, when, and with what
outcome". They should never be lost, rarely be verbose, and always be
parseable.

We emit newline-delimited JSON. One line per tool call. Fields:

* ``ts`` — ISO 8601 UTC
* ``trace_id`` — correlates with OTel span
* ``tenant`` — multi-tenancy scope
* ``caller`` — agent subject
* ``delegator`` — human who authorised the agent (RFC 8693 actor claim)
* ``tool`` — tool name
* ``args_hash`` — sha256 of JSON-serialised args (never the args themselves —
                  arguments may contain PII; the hash is for correlation)
* ``duration_ms``
* ``status`` — ok / error
* ``error_code`` — when status = error

Compliance frameworks expect retention policies measured in years. In
production you ship this file to your SIEM (Splunk, Datadog, Chronicle)
and let *it* own durability.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog


class AuditLogger:
    """Appends JSON lines to a file plus mirrors to stdout for collectors."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Reconfigure structlog to JSON for the audit channel specifically.
        self._logger = structlog.wrap_logger(
            structlog.get_logger("atlas.audit"),
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.JSONRenderer(),
            ],
        )

    def record(
        self,
        *,
        trace_id: str | None,
        tenant: str,
        caller: str,
        delegator: str | None,
        tool: str,
        arguments: dict[str, Any],
        duration_ms: float,
        status: str,
        error_code: str | None = None,
    ) -> None:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "trace_id": trace_id,
            "tenant": tenant,
            "caller": caller,
            "delegator": delegator,
            "tool": tool,
            "args_hash": _hash_args(arguments),
            "duration_ms": round(duration_ms, 3),
            "status": status,
            "error_code": error_code,
        }
        # File sink for durability.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
        # Stdout mirror for docker / k8s log collectors.
        self._logger.info("tool_call", **event)


def _hash_args(arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, sort_keys=True, default=str).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()
