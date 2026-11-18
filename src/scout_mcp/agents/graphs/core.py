"""The steps both copilot flows share: write claims, check them, revise once, finish.

``draft`` and ``ask`` differ only in how they gather evidence. Each supplies its own front
steps, which fill ``DraftRun.evidence``; this module supplies the rest and wires them into one
LangGraph state graph:

    front steps -> draft -> check -> finalize
                                 \\-> revise -> check

The graph only connects the steps. Everything that must not sit in graph state (the MCP client,
the model clients, the raw evidence, the call counter) lives in ``DraftRun``. State carries
counters, the outcome and the check results, so a trace or a later checkpoint shows the shape of
a run and none of its customer data.

Whatever goes wrong (no key, no budget, a model error, an unreadable reply) ends in
``evidence_pack``: the caller shows the facts the tools returned instead of a draft.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from scout_mcp.agents.claims import DraftOut, Evidence, assemble, check_draft
from scout_mcp.agents.llm import CallBudget, FinalLLMError, LLMUnavailable, invoke_models, parse_json_lenient
from scout_mcp.agents.mcp_client import wrap_untrusted
from scout_mcp.agents.prompts import DRAFT_SYSTEM
from scout_mcp.agents.redact import Redactor
from scout_mcp.agents.sla import TenantSla

RECURSION_LIMIT = 12  # a backstop: the longest real path is seven steps
REPAIR = ("That was not a single valid JSON object of the shape asked for. "
          "Reply with only the JSON object, nothing else.")


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
    sla: TenantSla | None = None
    started: float = field(default_factory=time.monotonic)
    counter: list[int] = field(default_factory=lambda: [0])
    tool_calls: int = 0
    context: dict | None = None
    evidence: list[Evidence] = field(default_factory=list)
    # Tool calls that failed or were refused, as "tool: code". Shown to the person, never cited.
    errors: list[str] = field(default_factory=list)
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
    errors: list[str] = field(default_factory=list)


def evidence_prompt(question: str, evidence: list[Evidence]) -> str:
    lines = "\n".join(f"[S{e.id}] {e.text}" for e in evidence)
    return (f"Customer question:\n{question}\n\nEvidence:\n{wrap_untrusted(lines)}\n\n"
            "Reply with the JSON object.")


def stop(run: DraftRun, reason: str) -> dict:
    """State update that ends the run with the evidence pack."""
    return {"outcome": "evidence_pack", "reason": reason, "llm_calls": run.counter[0]}


async def ask_model(run: DraftRun, messages: list, schema: type[BaseModel]) -> tuple[BaseModel | None, dict]:
    """One counted model call, with one counted repair when the reply is not the JSON asked for.

    Returns the parsed reply and a state update, or None and the update that ends the run.
    """
    for _ in range(2):
        remaining = run.remaining_ms()
        if remaining is not None and remaining <= 0:
            return None, stop(run, "time_budget")
        try:
            text = await invoke_models(run.models, messages, run.counter, limit=run.max_llm_calls)
        except CallBudget:
            return None, stop(run, "call_budget")
        except (FinalLLMError, LLMUnavailable) as exc:
            return None, stop(run, f"model: {type(exc).__name__}")
        text = run.redactor.restore(text)
        try:
            return schema.model_validate(parse_json_lenient(text)), {"llm_calls": run.counter[0]}
        except (ValueError, ValidationError):
            messages = messages + [AIMessage(content=run.redactor.redact(text)[:2000]),
                                   HumanMessage(content=REPAIR)]
    return None, stop(run, "the model's reply was unreadable")


def assemble_graph(run: DraftRun, front: list[tuple[str, Callable[[DraftState], Awaitable[dict]]]]):
    """Wire ``front`` steps, then the shared ones. A step that sets ``outcome`` ends the run."""

    async def draft(state: DraftState) -> dict:
        prompt = run.redactor.redact(evidence_prompt(state["question"], run.evidence))
        out, update = await ask_model(
            run, [SystemMessage(content=DRAFT_SYSTEM), HumanMessage(content=prompt)], DraftOut)
        run.out = out or run.out
        return update

    async def check(state: DraftState) -> dict:
        return {"problems": check_draft(run.out, run.evidence)}

    async def revise(state: DraftState) -> dict:
        issues = "\n".join(f"- {p}" for p in state["problems"])
        previous = run.out.model_dump_json() if run.out else "{}"
        prompt = run.redactor.redact(
            f"{evidence_prompt(state['question'], run.evidence)}\n\nYour previous reply was rejected:\n"
            f"{previous}\nProblems found by the checks:\n{issues}\nWrite a corrected JSON object.")
        out, update = await ask_model(
            run, [SystemMessage(content=DRAFT_SYSTEM), HumanMessage(content=prompt)], DraftOut)
        run.out = out or run.out
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
    steps = [*front, ("draft", draft)]
    for name, fn in [*steps, ("check", check), ("revise", revise), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, steps[0][0])
    names = [name for name, _ in steps] + ["check"]
    for here, there in zip(names, names[1:]):
        g.add_conditional_edges(here, stop_or(there), {"end": END, there: there})
    g.add_conditional_edges("check", after_check, {"revise": "revise", "finalize": "finalize"})
    g.add_conditional_edges("revise", stop_or("check"), {"end": END, "check": "check"})
    g.add_edge("finalize", END)
    return g.compile()


async def execute(run: DraftRun, front: list, customer_id: str, question: str) -> DraftResult:
    """Run the graph and report what happened."""
    state = await assemble_graph(run, front).ainvoke(
        {"customer_id": customer_id, "question": question, "llm_calls": 0, "revisions": 0},
        config={"recursion_limit": RECURSION_LIMIT})
    outcome = state["outcome"]
    text = assemble(run.out) if run.out and outcome in ("approved", "flagged") else ""
    return DraftResult(outcome=outcome, text=text, reason=state.get("reason"),
                       problems=state.get("problems") or [], llm_calls=run.counter[0],
                       tool_calls=run.tool_calls, evidence=run.evidence, context=run.context,
                       errors=run.errors)
