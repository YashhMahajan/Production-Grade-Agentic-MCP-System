"""Claim-level drafts and the checks that decide whether one may be shown.

The model writes claims, each pointing at the evidence it rests on. Code, not another
model, then checks every claim: the cited evidence exists, every number, date, amount
and contact detail in the claim appears in that evidence, nothing commits the company
to an outcome, and the draft is short enough to read. None of this needs the model or
a framework, so it is the same whatever runs the steps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

from scout_mcp.agents.brief import _age_days
from scout_mcp.agents.redact import _PATTERNS

MAX_WORDS = 180

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_COMMITMENTS = [
    re.compile(r"\b(?:we|i)(?:'ll| will| can)\s+(?:refund|credit|replace|waive|cancel|reimburse)", re.I),
    re.compile(r"\bguarantee", re.I),
    re.compile(r"\bpromise", re.I),
    re.compile(r"\b(?:has|have) been (?:refunded|credited|approved|waived)", re.I),
    re.compile(r"\bwill be (?:refunded|credited|approved|waived|replaced)", re.I),
]


class Claim(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    cites: list[int] = Field(default_factory=list)


class DraftOut(BaseModel):
    claims: list[Claim] = Field(default_factory=list, max_length=12)
    needs_approval: bool = False
    insufficient: bool = False


@dataclass
class Evidence:
    id: int
    kind: str
    text: str


def build_evidence(context: dict, now: datetime) -> list[Evidence]:
    """Number the facts in a ``customer.build_context`` result as S1, S2, ... in a fixed order."""
    items: list[tuple[str, str]] = []
    profile = context.get("profile") or {}
    if profile.get("name") or profile.get("tier"):
        since = f", customer since {profile['created_at'][:10]}" if profile.get("created_at") else ""
        items.append(("profile", f"Customer {profile.get('name') or 'unknown'} ({context.get('customer_id')}), "
                                 f"tier {profile.get('tier') or 'unknown'}{since}"))
    for o in context.get("orders") or []:
        items.append(("order", f"Order {o['id']}: status {o['status']}, total "
                               f"{o['total_cents'] / 100:.2f} {o['currency']}, placed {o['created_at'][:10]}"))
    for t in context.get("tickets") or []:
        age = _age_days(t.get("created_at"), now)
        opened = f", opened {t['created_at'][:10]} ({age} days ago)" if age is not None else ""
        items.append(("ticket", f"Ticket {t['id']}: {t.get('priority') or 'unknown'} priority, "
                                f"{t.get('status') or 'unknown'}{opened}, subject: {t.get('subject') or ''}"))
    for d in context.get("docs") or []:
        body = (d.get("body") or d.get("preview") or "")[:300]
        items.append(("document", f"Document {d.get('title') or d.get('id')}: {body}"))
    return [Evidence(i, kind, text) for i, (kind, text) in enumerate(items, start=1)]


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def _contacts(text: str) -> set[str]:
    return {m.group(0) for _, pattern in _PATTERNS for m in pattern.finditer(text)}


def check_draft(out: DraftOut, evidence: list[Evidence]) -> list[str]:
    """Why this draft may not be shown, one line each. Empty means it passed."""
    problems: list[str] = []
    if not out.claims:
        return [] if out.insufficient else ["the draft makes no claims"]
    by_id = {e.id: e.text for e in evidence}
    words = 0
    for n, claim in enumerate(out.claims, start=1):
        words += len(claim.text.split())
        bad = [c for c in claim.cites if c not in by_id]
        if not claim.cites:
            problems.append(f"claim {n} cites no evidence")
        if bad:
            problems.append(f"claim {n} cites [S{bad[0]}] but there are only {len(by_id)} evidence items")
        cited = " ".join(by_id[c] for c in claim.cites if c in by_id)
        missing = sorted(_numbers(claim.text) - _numbers(cited))
        if missing:
            problems.append(f"claim {n} states {', '.join(missing)}, which its cited evidence does not")
        loose = sorted(_contacts(claim.text) - _contacts(cited))
        if loose:
            problems.append(f"claim {n} contains a contact detail that its evidence does not")
        if any(p.search(claim.text) for p in _COMMITMENTS):
            problems.append(f"claim {n} commits the company to an outcome")
    if words > MAX_WORDS:
        problems.append(f"the draft is {words} words, over the limit of {MAX_WORDS}")
    return problems


def assemble(out: DraftOut) -> str:
    """The claims as reply text, each followed by the evidence it cites."""
    if not out.claims:
        return "I do not have enough evidence to answer this."
    parts = [f"{c.text.strip()} {' '.join(f'[S{i}]' for i in c.cites)}".strip() for c in out.claims]
    return " ".join(parts)
