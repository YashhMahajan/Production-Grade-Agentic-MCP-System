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
# 1st agent - planner
# 2nd agent - retriever
# 3rd agent - synthesizer
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

import structlog

from scout_mcp.agents.base import LLM, AgentOutputError, AgentRun, BudgetExceeded
from scout_mcp.agents.critic import CriticAgent, Verdict
from scout_mcp.agents.mcp_client import ScoutMCPClient
from scout_mcp.agents.planner import Plan, PlannerAgent
from scout_mcp.agents.retriever import RetrieverAgent, RetrievalResult
from scout_mcp.agents.synthesizer import Draft, SynthesizerAgent

log = structlog.get_logger("scout.orchestrator")


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
    # Why the run stopped early ("token_budget", "time_budget"), or None for a full run.
    degraded: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft": self.draft,
            "approved": self.approved,
            "citations": self.citations,
            "run_id": self.run_id,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tool_calls": self.tool_calls,
            "degraded": self.degraded,
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


def citation_problems(draft: Draft, n_findings: int) -> list[str]:
    """Citations the draft makes that do not match a finding, or no citations at all."""
    cited = sorted({int(c[2:-1]) for c in draft.citations})
    problems = [f"cites [S{i}] but there are only {n_findings} findings"
                for i in cited if i < 1 or i > n_findings]
    if not cited:
        problems.append("cites no findings")
    return problems


class SupportCopilot:
    """The concrete enterprise product.

    Usage::

        async with ScoutMCPClient(url, token, tenant="acme") as mcp:
            copilot = SupportCopilot(mcp)
            response = await copilot.answer("Why was my order CUST-1234's refund delayed?")
            print(response.draft)
    """

    def __init__(
        self,
        mcp_client: ScoutMCPClient,
        llm: LLM | None = None,
        max_retriever_iterations: int = 6,
        token_budget: int | None = None,
        timeout_s: float | None = None,
    ):
        # One run may spend this many tokens and this many seconds, across all four agents.
        self.token_budget = token_budget or int(os.environ.get("SCOUT_AGENT_TOKEN_BUDGET", "60000"))
        self.timeout_s = timeout_s or float(os.environ.get("SCOUT_AGENT_TIMEOUT_S", "90"))
        self.llm = llm or LLM()
        self.mcp = mcp_client
        self.planner = PlannerAgent(self.llm)
        self.retriever = RetrieverAgent(self.llm, mcp_client, max_iterations=max_retriever_iterations)
        self.synthesizer = SynthesizerAgent(self.llm)
        self.critic = CriticAgent(self.llm)

    async def answer(self, question: str) -> CopilotResponse:
        run = AgentRun(max_tokens=self.token_budget, timeout_s=self.timeout_s)
        progress: dict[str, Any] = {}
        try:
            return await self._answer(run, question, progress)
        except BudgetExceeded as exc:
            log.warning("copilot_budget_exceeded", run_id=run.run_id, reason=exc.reason)
            return self._degraded(run, exc.reason, progress)

    async def _answer(self, run: AgentRun, question: str, progress: dict) -> CopilotResponse:
        log.info("copilot_start", run_id=run.run_id, question=question[:200])

        # 1. Plan. An unreadable plan is not fatal: the retriever works from the question.
        try:
            plan = await self.planner.act(run, question=question)
        except AgentOutputError:
            plan = Plan(needs=[], customer_id_required=False, notes="planner output unreadable",
                        customer_id=PlannerAgent._extract_customer_id(question))
        progress["plan"] = plan
        log.info("plan_ready", run_id=run.run_id, needs=len(plan.needs),
                 customer_id=plan.customer_id)

        if plan.customer_id_required and not plan.customer_id:
            return self._short_circuit(run, plan,
                "I couldn't find a customer id in that question. "
                "Please include it (e.g. CUST-1234) and I'll try again.")

        # 2. Retrieve.
        retrieval = await self.retriever.act(run, plan=plan, question=question)
        progress["retrieval"] = retrieval
        run.check()  # a retrieval that spent the budget is a degraded run, not "nothing found"
        log.info("retrieval_done", run_id=run.run_id,
                 findings=len(retrieval.findings), iterations=retrieval.iterations_used)

        if not retrieval.findings:
            return self._short_circuit(run, plan,
                "I wasn't able to find any relevant information in our systems. "
                "Please double-check the customer id and question.")

        # 3. Synthesize + critique with one revise loop.
        draft = await self.synthesizer.act(run, question=question, findings=retrieval.findings)
        verdict = await self._review(run, question, retrieval, draft)

        if not verdict.approved:
            log.info("critic_requested_revision", run_id=run.run_id, issues=verdict.issues)
            draft = await self._revise(run, question, retrieval, draft, verdict)
            verdict = await self._review(run, question, retrieval, draft)

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

    async def _review(self, run, question, retrieval, draft) -> Verdict:
        """The model critic, then a check no model is trusted to do: do the citations exist?

        An unreadable critic reply is a rejection, never an approval.
        """
        try:
            verdict = await self.critic.act(
                run, question=question, findings=retrieval.findings, draft=draft
            )
        except AgentOutputError:
            verdict = Verdict(approved=False, issues=["the critic's reply was unreadable"],
                              revision_hints=None)
        problems = citation_problems(draft, len(retrieval.findings))
        if problems:
            hint = (f"Cite only [S1] to [S{len(retrieval.findings)}], each next to the claim it supports.")
            return Verdict(approved=False, issues=[*verdict.issues, *problems],
                           revision_hints=verdict.revision_hints or hint)
        return verdict

    @staticmethod
    def _degraded(run: AgentRun, reason: str, progress: dict) -> CopilotResponse:
        """The run hit its budget. Say so, and return what it had by then."""
        retrieval = progress.get("retrieval") or RetrievalResult()
        found = len(retrieval.findings)
        words = {"token_budget": "token budget", "time_budget": "time limit"}.get(reason, reason)
        return CopilotResponse(
            draft=(f"I ran out of my {words} before I could write a reliable answer "
                   f"({found} finding{'s' if found != 1 else ''} gathered). "
                   "Please try a narrower question."),
            approved=False,
            citations=[],
            plan=progress.get("plan") or Plan(needs=[], customer_id_required=False, notes=""),
            retrieval=retrieval,
            critic=Verdict(approved=False, issues=[f"stopped: {reason}"], revision_hints=None),
            run_id=run.run_id,
            tokens_in=run.tokens_in,
            tokens_out=run.tokens_out,
            tool_calls=run.tool_calls,
            degraded=reason,
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
