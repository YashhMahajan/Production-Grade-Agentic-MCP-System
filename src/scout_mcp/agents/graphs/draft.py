"""The draft-reply flow as a LangGraph state graph.

    load_evidence -> draft -> check -> finalize
                                  \\-> revise -> check

The graph only connects the steps. Each step is a small function over a ``DraftRun``,
which holds everything that must not sit in graph state: the MCP client, the model
clients, the raw evidence and the call counter. State carries only counters, the outcome
and the check results, so a trace or a later checkpoint shows the shape of a run and
none of its customer data.

Whatever goes wrong (no key, no budget, a model error, an unreadable reply) ends in
``evidence_pack``: the caller shows the facts the tools returned instead of a draft.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from scout_mcp.agents.base import _parse_json_lenient
from scout_mcp.agents.claims import DraftOut, Evidence, assemble, build_evidence, check_draft
from scout_mcp.agents.llm import CallBudget, FinalLLMError, LLMUnavailable, invoke_models
from scout_mcp.agents.mcp_client import wrap_untrusted
from scout_mcp.agents.prompts import DRAFT_SYSTEM
from scout_mcp.agents.redact import Redactor

RECURSION_LIMIT = 12  # a backstop: the longest real path is eight steps


class DraftState(TypedDict, total=False):
    customer_id: str
    question: str
    llm_calls: int
    revisions: int
    outcome: str  # approved | flagged | evidence_pack | error
    reason: str
    problems: list[str]


@dataclass
class DraftRun:
    mcp: Any
    models: list
    now: datetime
    max_llm_calls: int = 3
    timeout_s: float | None = None
    started: float = field(default_factory=time.monotonic)
    counter: list[int] = field(default_factory=lambda: [0])
    tool_calls: int = 0
    context: dict | None = None
    evidence: list[Evidence] = field(default_factory=list)
    out: DraftOut | None = None
    redactor: Redactor = field(default_factory=Redactor)

    def remaining_ms(self) -> int | None:
        if self.timeout_s is None:
            return None
        return int((self.timeout_s - (time.monotonic() - self.started)) * 1000)


@dataclass
class DraftResult:
    outcome: str
    text: str
    reason: str | None
    problems: list[str]
    llm_calls: int
    tool_calls: int
    evidence: list[Evidence]
    context: dict | None


def _prompt(question: str, evidence: list[Evidence]) -> str:
    lines = "\n".join(f"[S{e.id}] {e.text}" for e in evidence)
    return (f"Customer question:\n{question}\n\nEvidence:\n{wrap_untrusted(lines)}\n\n"
            "Reply with the JSON object.")


def build_draft_graph(run: DraftRun):
    async def load_evidence(state: DraftState) -> dict:
        run.tool_calls += 1
        result = await run.mcp.call_tool(
            "customer.build_context",
            {"customer_id": state["customer_id"], "question": state["question"][:2000]},
            deadline_ms=run.remaining_ms())
        if not result.ok:
            return {"outcome": "error", "reason": f"lookup failed: {result.error_code}"}
        run.context = result.value
        run.evidence = build_evidence(result.value, run.now)
        if not run.evidence:
            return {"outcome": "evidence_pack", "reason": "no evidence found"}
        return {}

    async def ask(messages: list, state: DraftState) -> dict:
        """One counted model call, with one counted repair when the reply is not the JSON asked for."""
        for _ in range(2):
            remaining = run.remaining_ms()
            if remaining is not None and remaining <= 0:
                return {"outcome": "evidence_pack", "reason": "time_budget", "llm_calls": run.counter[0]}
            try:
                text = await invoke_models(run.models, messages, run.counter, limit=run.max_llm_calls)
            except CallBudget:
                return {"outcome": "evidence_pack", "reason": "call_budget", "llm_calls": run.counter[0]}
            except (FinalLLMError, LLMUnavailable) as exc:
                return {"outcome": "evidence_pack", "reason": f"model: {type(exc).__name__}",
                        "llm_calls": run.counter[0]}
            text = run.redactor.restore(text)
            try:
                run.out = DraftOut.model_validate(_parse_json_lenient(text))
                return {"llm_calls": run.counter[0]}
            except (ValueError, ValidationError):
                messages = messages + [
                    AIMessage(content=run.redactor.redact(text)[:2000]),
                    HumanMessage(content="That was not a single valid JSON object of the shape asked for. "
                                         "Reply with only the JSON object, nothing else.")]
        return {"outcome": "evidence_pack", "reason": "the model's reply was unreadable",
                "llm_calls": run.counter[0]}

    async def draft(state: DraftState) -> dict:
        prompt = run.redactor.redact(_prompt(state["question"], run.evidence))
        return await ask([SystemMessage(content=DRAFT_SYSTEM), HumanMessage(content=prompt)], state)

    async def check(state: DraftState) -> dict:
        return {"problems": check_draft(run.out, run.evidence)}

    async def revise(state: DraftState) -> dict:
        issues = "\n".join(f"- {p}" for p in state["problems"])
        previous = run.out.model_dump_json() if run.out else "{}"
        prompt = run.redactor.redact(
            f"{_prompt(state['question'], run.evidence)}\n\nYour previous reply was rejected:\n{previous}\n"
            f"Problems found by the checks:\n{issues}\nWrite a corrected JSON object.")
        update = await ask([SystemMessage(content=DRAFT_SYSTEM), HumanMessage(content=prompt)], state)
        return {**update, "revisions": state.get("revisions", 0) + 1}

    async def finalize(state: DraftState) -> dict:
        return {"outcome": "approved" if not state["problems"] else "flagged"}

    def stop_or(next_node: str):
        return lambda state: "end" if state.get("outcome") else next_node

    def after_check(state: DraftState) -> str:
        if state["problems"] and state.get("revisions", 0) < 1 and run.counter[0] < run.max_llm_calls:
            return "revise"
        return "finalize"

    g = StateGraph(DraftState)
    for name, fn in [("load_evidence", load_evidence), ("draft", draft), ("check", check),
                     ("revise", revise), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "load_evidence")
    g.add_conditional_edges("load_evidence", stop_or("draft"), {"end": END, "draft": "draft"})
    g.add_conditional_edges("draft", stop_or("check"), {"end": END, "check": "check"})
    g.add_conditional_edges("check", after_check, {"revise": "revise", "finalize": "finalize"})
    g.add_conditional_edges("revise", stop_or("check"), {"end": END, "check": "check"})
    g.add_edge("finalize", END)
    return g.compile()


async def run_draft(mcp, models: list, customer_id: str, question: str, now: datetime,
                    max_llm_calls: int = 3, timeout_s: float | None = None) -> DraftResult:
    run = DraftRun(mcp=mcp, models=models, now=now, max_llm_calls=max_llm_calls, timeout_s=timeout_s)
    state = await build_draft_graph(run).ainvoke(
        {"customer_id": customer_id, "question": question, "llm_calls": 0, "revisions": 0},
        config={"recursion_limit": RECURSION_LIMIT})
    outcome = state["outcome"]
    text = assemble(run.out) if run.out and outcome in ("approved", "flagged") else ""
    return DraftResult(outcome=outcome, text=text, reason=state.get("reason"),
                       problems=state.get("problems") or [], llm_calls=run.counter[0],
                       tool_calls=run.tool_calls, evidence=run.evidence, context=run.context)
