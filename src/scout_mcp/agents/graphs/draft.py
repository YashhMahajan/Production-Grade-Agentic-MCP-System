"""``draft``: a cited reply for one customer, from one fixed tool call.

The model chooses no tool. ``customer.build_context`` runs, its facts become numbered
evidence, and the shared steps in ``core`` write, check and (once) revise the claims.
"""

from __future__ import annotations

from datetime import datetime

from scout_mcp.agents.claims import build_evidence
from scout_mcp.agents.graphs.core import DraftResult, DraftRun, DraftState, assemble_graph, execute
from scout_mcp.agents.sla import TenantSla

__all__ = ["DraftResult", "DraftRun", "build_draft_graph", "run_draft"]


def _front(run: DraftRun):
    async def load_evidence(state: DraftState) -> dict:
        run.tool_calls += 1
        result = await run.mcp.call_tool(
            "customer.build_context",
            {"customer_id": state["customer_id"], "question": state["question"][:2000]},
            deadline_ms=run.remaining_ms())
        if not result.ok:
            return {"outcome": "error", "reason": f"lookup failed: {result.error_code}"}
        run.context = result.value
        run.evidence = build_evidence(result.value, run.now, run.sla)
        if not run.evidence:
            return {"outcome": "evidence_pack", "reason": "no evidence found"}
        return {}

    return [("load_evidence", load_evidence)]


def build_draft_graph(run: DraftRun):
    return assemble_graph(run, _front(run))


async def run_draft(mcp, models: list, customer_id: str, question: str, now: datetime,
                    max_llm_calls: int = 3, timeout_s: float | None = None,
                    sla: TenantSla | None = None, trace: dict | None = None) -> DraftResult:
    run = DraftRun(mcp=mcp, models=models, now=now, max_llm_calls=max_llm_calls, timeout_s=timeout_s, sla=sla)
    return await execute(run, _front(run), customer_id, question, trace)
