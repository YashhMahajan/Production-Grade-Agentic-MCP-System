"""The customer brief: what the tools already know, laid out for a support agent.

No model is involved. The text comes straight from the ``customer.build_context``
result and the flags come from fixed rules, so it works without a key and without
any quota.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from scout_mcp.agents.sla import TenantSla, age_hours


@dataclass
class Brief:
    text: str
    flags: list[str] = field(default_factory=list)


def make_brief(context: dict, now: datetime, sla: TenantSla | None = None) -> Brief:
    profile = context.get("profile") or {}
    orders = context.get("orders") or []
    tickets = context.get("tickets") or []
    docs = context.get("docs") or []
    errors = context.get("partial_errors") or {}
    flags: list[str] = []

    lines = [f"{profile.get('name') or 'Unknown customer'} ({context.get('customer_id')})"
             f" - tier {profile.get('tier') or 'unknown'}"]
    lines.append("")
    lines.append("Orders")
    for o in orders:
        money = f"{o['total_cents'] / 100:.2f} {o['currency']}"
        lines.append(f"  {o['id']}  {o['status']}  {money}  {o['created_at'][:10]}")
    if not orders:
        lines.append("  none")

    lines.append("")
    lines.append("Open tickets")
    for t in tickets:
        hours = age_hours(t.get("created_at"), now)
        age_text = f"{int(hours // 24)} days" if hours is not None else "age unknown"
        lines.append(f"  {t['id']}  {t.get('priority') or '-'}  {t.get('status') or '-'}  "
                     f"{age_text}  {t.get('subject') or ''}")
        if t.get("priority") == "high":
            flags.append(f"open high-priority ticket {t['id']}")
        status = sla.status(t, profile.get("tier"), now) if sla else None
        if status and status.state != "ok":
            flags.append(f"ticket {t['id']}: {status.describe()}")
    if not tickets:
        lines.append("  none")

    if docs:
        lines.append("")
        lines.append(f"Related documents: {len(docs)}")

    for part, why in errors.items():
        flags.append(f"incomplete: {part} ({why})")
    if flags:
        lines.append("")
        lines.append("Flags")
        lines.extend(f"  - {f}" for f in flags)
    return Brief(text="\n".join(lines), flags=flags)


def make_activity(value: dict) -> str:
    """What ``customer.activity_since`` returned, as text."""
    lines = [f"New for {value['customer_id']} since {value['since']}", "", "Orders"]
    for o in value.get("orders") or []:
        lines.append(f"  {o['id']}  {o['status']}  {o['total_cents'] / 100:.2f} {o['currency']}  {o['created_at'][:10]}")
    if not value.get("orders"):
        lines.append("  none")
    lines += ["", "Tickets"]
    for t in value.get("tickets") or []:
        lines.append(f"  {t['id']}  {t.get('priority') or '-'}  {t.get('status') or '-'}  "
                     f"{(t.get('created_at') or '')[:10]}  {t.get('subject') or ''}")
    if not value.get("tickets"):
        lines.append("  none")
    for part, why in (value.get("partial_errors") or {}).items():
        lines.append(f"\nincomplete: {part} ({why})")
    return "\n".join(lines)
