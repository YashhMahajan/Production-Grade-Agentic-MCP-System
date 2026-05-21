"""Planner agent — turns a user question into a retrieval plan."""

from __future__ import annotations

import re
from dataclasses import dataclass

from atlas_mcp.agents.base import Agent, AgentRun
from atlas_mcp.agents.prompts import PLANNER_SYSTEM


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


# A conservative pattern — alphanumerics, dashes, underscores, 3-64 chars.
_CUSTOMER_ID_RE = re.compile(r"\b(cust|customer|account|acct)[_-]?([A-Za-z0-9\-_]{3,64})\b", re.I)


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
            needs=[PlanStep(id=n["id"], description=n["description"],
                            priority=int(n.get("priority", 2)))
                   for n in data.get("needs", [])],
            customer_id_required=bool(data.get("customer_id_required")),
            notes=str(data.get("notes", "")),
            customer_id=customer_id,
        )

    @staticmethod
    def _extract_customer_id(question: str) -> str | None:
        match = _CUSTOMER_ID_RE.search(question)
        return match.group(2) if match else None
