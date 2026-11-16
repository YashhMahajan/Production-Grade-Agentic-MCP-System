"""The customer brief: what the tools already know, laid out for a support agent.

No model is involved. The text comes straight from the ``customer.build_context``
result and the flags come from fixed rules, so it works without a key and without
any quota.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# An open or pending ticket this many days old is flagged. The per-tenant SLA rules replace this later.
STALE_DAYS = 3


@dataclass
class Brief:
    text: str
    flags: list[str] = field(default_factory=list)


def _age_days(created_at: str | None, now: datetime) -> int | None:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max((now - created).days, 0)


def make_brief(context: dict, now: datetime) -> Brief:
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
        age = _age_days(t.get("created_at"), now)
        age_text = f"{age} days" if age is not None else "age unknown"
        lines.append(f"  {t['id']}  {t.get('priority') or '-'}  {t.get('status') or '-'}  "
                     f"{age_text}  {t.get('subject') or ''}")
        if t.get("priority") == "high":
            flags.append(f"open high-priority ticket {t['id']}")
        if age is not None and age >= STALE_DAYS:
            flags.append(f"ticket {t['id']} is {age} days old")
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
