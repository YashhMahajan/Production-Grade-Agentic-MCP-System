"""Planner agent — turns a user question into a retrieval plan."""

# agent 1 - planner

from __future__ import annotations

import re
from dataclasses import dataclass

from scout_mcp.agents.base import Agent, AgentRun
from scout_mcp.agents.prompts import PLANNER_SYSTEM


@dataclass
class PlanStep:
    id: str
    description: str
    priority: int


@dataclass
class Plan:
    needs: list[PlanStep]
    customer_id_required: bool
    notes: str
    # Parsed out of the user message by the planner if present.
    customer_id: str | None = None


# Matches ids shaped like CUST-1001. The separator is required: without it the
# pattern matched ordinary words ("customers", "accounting") as ids.
_CUSTOMER_ID_RE = re.compile(r"\b(?:cust|customer|acct|account)[_-][A-Za-z0-9]{2,60}\b", re.IGNORECASE)


def _priority(value) -> int:
    try:
        return min(3, max(1, int(value)))
    except (TypeError, ValueError):
        return 2


class PlannerAgent(Agent):
    name = "planner"
    system_prompt = PLANNER_SYSTEM

    async def act(self, run: AgentRun, *, question: str) -> Plan:  # type: ignore[override]
        customer_id = self._extract_customer_id(question)

        data = await self._complete_json(
            run,
            messages=[{
                "role": "user",
                "content": f"Customer question:\n{question}\n\n"
                           f"Return a retrieval plan as JSON.",
            }],
            max_tokens=400,
        )
        return Plan(
            needs=[PlanStep(id=str(n.get("id") or f"n{i}"), description=str(n["description"]),
                            priority=_priority(n.get("priority")))
                   for i, n in enumerate(data.get("needs") or [], start=1)
                   if isinstance(n, dict) and n.get("description")],
            customer_id_required=bool(data.get("customer_id_required")),
            notes=str(data.get("notes", "")),
            customer_id=customer_id,
        )

    @staticmethod
    def _extract_customer_id(question: str) -> str | None:
        match = _CUSTOMER_ID_RE.search(question)
        # Ids are stored upper-case (CUST-1001), whatever case the user typed.
        return match.group(0).upper() if match else None
