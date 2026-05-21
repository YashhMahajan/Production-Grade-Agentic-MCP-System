"""Multi-agent orchestrator — the thing you actually call from a backend.

The flow is explicit:

    Planner → Retriever → Synthesizer → Critic ──approve──▶ return draft
                                             └─revise──▶ Synthesizer (1 more shot)

At most one revise loop. Two reasons:

* A draft that cannot pass the critic after one revision is usually missing
  information the retriever did not find. Looping indefinitely does not fix
  that; it just burns tokens.
* Bounded latency. A user-facing copilot that takes 90 seconds to answer
  is a copilot that nobody uses.

Every agent shares the same :class:`AgentRun` so you get per-turn token
counts and per-turn tool call counts in one place.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

import structlog

from atlas_mcp.agents.base import LLM, AgentRun
from atlas_mcp.agents.critic import CriticAgent, Verdict
from atlas_mcp.agents.mcp_client import AtlasMCPClient
from atlas_mcp.agents.planner import Plan, PlannerAgent
from atlas_mcp.agents.retriever import RetrieverAgent, RetrievalResult
from atlas_mcp.agents.synthesizer import Draft, SynthesizerAgent

log = structlog.get_logger("atlas.orchestrator")


@dataclass
class CopilotResponse:
    draft: str
    approved: bool
    citations: list[str]
    plan: Plan
    retrieval: RetrievalResult
    critic: Verdict
    run_id: str
    tokens_in: int
    tokens_out: int
    tool_calls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft": self.draft,
            "approved": self.approved,
            "citations": self.citations,
            "run_id": self.run_id,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tool_calls": self.tool_calls,
            "plan": {
                "customer_id": self.plan.customer_id,
                "needs": [asdict(n) for n in self.plan.needs],
                "notes": self.plan.notes,
            },
            "retrieval": {
                "iterations_used": self.retrieval.iterations_used,
                "exceeded_budget": self.retrieval.exceeded_budget,
                "findings_count": len(self.retrieval.findings),
            },
            "critic": asdict(self.critic),
        }


class SupportCopilot:
    """The concrete enterprise product.

    Usage::

        async with AtlasMCPClient(url, token, tenant="acme") as mcp:
            copilot = SupportCopilot(mcp)
            response = await copilot.answer("Why was my order CUST-1234's refund delayed?")
            print(response.draft)
    """

    def __init__(
        self,
        mcp_client: AtlasMCPClient,
        llm: LLM | None = None,
        max_retriever_iterations: int = 6,
    ):
        self.llm = llm or LLM()
        self.mcp = mcp_client
        self.planner = PlannerAgent(self.llm)
        self.retriever = RetrieverAgent(self.llm, mcp_client, max_iterations=max_retriever_iterations)
        self.synthesizer = SynthesizerAgent(self.llm)
        self.critic = CriticAgent(self.llm)

    async def answer(self, question: str) -> CopilotResponse:
        run = AgentRun()
        log.info("copilot_start", run_id=run.run_id, question=question[:200])

        # 1. Plan.
        plan = await self.planner.act(run, question=question)
        log.info("plan_ready", run_id=run.run_id, needs=len(plan.needs),
                 customer_id=plan.customer_id)

        if plan.customer_id_required and not plan.customer_id:
            return self._short_circuit(run, plan,
                "I couldn't find a customer id in that question. "
                "Please include it (e.g. CUST-1234) and I'll try again.")

        # 2. Retrieve.
        retrieval = await self.retriever.act(run, plan=plan, question=question)
        log.info("retrieval_done", run_id=run.run_id,
                 findings=len(retrieval.findings), iterations=retrieval.iterations_used)

        if not retrieval.findings:
            return self._short_circuit(run, plan,
                "I wasn't able to find any relevant information in our systems. "
                "Please double-check the customer id and question.")

        # 3. Synthesize + critique with one revise loop.
        draft = await self.synthesizer.act(run, question=question, findings=retrieval.findings)
        verdict = await self.critic.act(
            run, question=question, findings=retrieval.findings, draft=draft
        )

        if not verdict.approved:
            log.info("critic_requested_revision", run_id=run.run_id, issues=verdict.issues)
            draft = await self._revise(run, question, retrieval, draft, verdict)
            verdict = await self.critic.act(
                run, question=question, findings=retrieval.findings, draft=draft
            )

        log.info(
            "copilot_done",
            run_id=run.run_id,
            approved=verdict.approved,
            tokens_in=run.tokens_in,
            tokens_out=run.tokens_out,
            tool_calls=run.tool_calls,
        )

        return CopilotResponse(
            draft=draft.text,
            approved=verdict.approved,
            citations=draft.citations,
            plan=plan,
            retrieval=retrieval,
            critic=verdict,
            run_id=run.run_id,
            tokens_in=run.tokens_in,
            tokens_out=run.tokens_out,
            tool_calls=run.tool_calls,
        )

    async def _revise(
        self,
        run: AgentRun,
        question: str,
        retrieval: RetrievalResult,
        first_draft: Draft,
        verdict: Verdict,
    ) -> Draft:
        # Inline the critic's hints into the synthesis prompt for a second shot.
        hint_block = (
            f"\n\nYOUR PREVIOUS DRAFT WAS REJECTED:\n{first_draft.text}\n\n"
            f"Issues flagged by the critic:\n"
            + "\n".join(f"- {i}" for i in verdict.issues)
            + (f"\n\nRevision hints: {verdict.revision_hints}" if verdict.revision_hints else "")
        )
        # The synthesizer's act() takes question + findings; we sneak the hint
        # in by mutating the question it sees. This keeps synthesizer.py
        # single-purpose.
        return await self.synthesizer.act(
            run, question=question + hint_block, findings=retrieval.findings
        )

    @staticmethod
    def _short_circuit(run: AgentRun, plan: Plan, message: str) -> CopilotResponse:
        return CopilotResponse(
            draft=message,
            approved=True,
            citations=[],
            plan=plan,
            retrieval=RetrievalResult(),
            critic=Verdict(approved=True, issues=[], revision_hints=None),
            run_id=run.run_id,
            tokens_in=run.tokens_in,
            tokens_out=run.tokens_out,
            tool_calls=run.tool_calls,
        )
