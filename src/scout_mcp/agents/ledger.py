"""A daily count of model calls, kept in a small private file.

The free OpenRouter tier allows about 50 requests a day. The copilot stops itself
well before that, so a run never discovers the limit by failing.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


class LedgerExhausted(Exception):
    """Today's allowance of model calls is spent."""


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Ledger:
    def __init__(self, path: Path | str | None = None, limit: int | None = None,
                 today: Callable[[], str] = _utc_day):
        self.path = Path(path) if path else Path.home() / ".scout" / "ledger.json"
        self.limit = limit if limit is not None else int(os.environ.get("SCOUT_LLM_DAILY_LIMIT", "40"))
        self._today = today

    def _used(self) -> int:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return 0
        return int(data.get("used", 0)) if data.get("day") == self._today() else 0

    def remaining(self) -> int:
        return max(self.limit - self._used(), 0)

    def spend(self, n: int) -> None:
        """Take ``n`` calls from today's allowance, or raise without taking any."""
        if n > self.remaining():
            raise LedgerExhausted(f"{n} calls wanted, {self.remaining()} left today")
        self.record(n)

    def record(self, n: int) -> None:
        """Count calls that already happened. Never refuses: the money is spent."""
        self._write(self._used() + n)

    def _write(self, used: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"day": self._today(), "used": used}, fh)
        os.replace(tmp, self.path)
