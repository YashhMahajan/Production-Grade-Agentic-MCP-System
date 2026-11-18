"""Response targets for tickets, read from ``config/sla.yaml``.

A target is the number of hours an open or pending ticket may wait. The copilot reports a
ticket as nearing its target at 75% of it and as past it beyond 100%. Nothing here calls a
model or a tool: it is arithmetic on a ticket's priority, status and creation time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

OPEN_STATUSES = ("open", "pending")
AT_RISK_FRACTION = 0.75
_TOP_LEVEL = {"default", "tiers", "tenants"}


class SlaError(ValueError):
    """The SLA file is not the shape this module reads."""


def age_hours(created_at: str | None, now: datetime) -> float | None:
    """Hours since an ISO 8601 time, or None when it is missing or unreadable."""
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max((now - created).total_seconds() / 3600, 0.0)


@dataclass(frozen=True)
class SlaStatus:
    target_hours: float
    open_hours: float
    state: str  # "ok", "at_risk" or "breached"

    def describe(self) -> str:
        return f"SLA target {self.target_hours:g}h, {self.state.replace('_', ' ')} ({int(self.open_hours)}h open)"


def _targets(node: object, where: str) -> dict[str, float]:
    if not isinstance(node, dict):
        raise SlaError(f"{where} must map a priority to hours")
    for priority, hours in node.items():
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
            raise SlaError(f"{where}.{priority} must be a positive number of hours")
    return {str(p): float(h) for p, h in node.items()}


class Sla:
    def __init__(self, data: dict):
        unknown = set(data) - _TOP_LEVEL
        if unknown:
            raise SlaError(f"unknown key(s) in the SLA file: {', '.join(sorted(unknown))}")
        self._default = _targets(data.get("default") or {}, "default")
        self._tiers = {t: _targets(v, f"tiers.{t}") for t, v in (data.get("tiers") or {}).items()}
        self._tenants = {}
        for name, node in (data.get("tenants") or {}).items():
            self._tenants[name] = (
                _targets(node.get("default") or {}, f"tenants.{name}.default"),
                {t: _targets(v, f"tenants.{name}.tiers.{t}") for t, v in (node.get("tiers") or {}).items()},
            )

    @classmethod
    def load(cls, path: str | Path | None = None) -> Sla:
        path = Path(path or os.environ.get("SCOUT_SLA_FILE", "config/sla.yaml"))
        return cls(yaml.safe_load(path.read_text()) or {})

    def for_tenant(self, tenant: str) -> TenantSla:
        return TenantSla(self, tenant)

    def hours(self, tenant: str, tier: str | None, priority: str | None) -> float | None:
        tenant_default, tenant_tiers = self._tenants.get(tenant, ({}, {}))
        for layer in (tenant_tiers.get(tier or ""), tenant_default, self._tiers.get(tier or ""), self._default):
            if layer and priority in layer:
                return layer[priority]
        return None


@dataclass(frozen=True)
class TenantSla:
    sla: Sla
    tenant: str

    def status(self, ticket: dict, tier: str | None, now: datetime) -> SlaStatus | None:
        """Where an open ticket stands against its target, or None when it has none to stand against."""
        if ticket.get("status") not in OPEN_STATUSES:
            return None
        target = self.sla.hours(self.tenant, tier, ticket.get("priority"))
        open_hours = age_hours(ticket.get("created_at"), now)
        if target is None or open_hours is None:
            return None
        state = "breached" if open_hours > target else "at_risk" if open_hours >= target * AT_RISK_FRACTION else "ok"
        return SlaStatus(target, open_hours, state)
