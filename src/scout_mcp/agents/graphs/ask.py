"""``ask``: a cited answer to a free-text question, from tools the model picks once.

    plan -> run_tools -> draft -> check -> ...        (the last three are in ``core``)

One model call proposes at most a few read-only tool calls. Code, not the model, decides what
runs: a tool off the allow-list is refused, the customer lookup is forced when the question names
a customer, and the calls that remain run in parallel through ``ScoutMCPClient``, so the server's
scopes, policy and deadline apply to each. What the tools return becomes numbered evidence. The
step that reads that evidence has no tools, so text in a ticket cannot cause a call.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from scout_mcp.agents.claims import Evidence, build_evidence
from scout_mcp.agents.graphs.core import DraftResult, DraftRun, DraftState, ask_model, execute, stop
from scout_mcp.agents.ids import extract_customer_id
from scout_mcp.agents.prompts import PLAN_SYSTEM
from scout_mcp.agents.sla import TenantSla

# Read-only tools the copilot may call. A second layer: the server's policy applies to every call too.
ALLOWED_TOOLS = frozenset({
    "customer.build_context", "customer.activity_since", "semantic_search", "hybrid_search",
    "postgres.query", "postgres.describe", "elasticsearch.search", "vector.search",
})
MAX_TOOL_CALLS = 4
RESULT_CHARS = 1500  # what one tool result may take up as evidence


class ToolCall(BaseModel):
    tool: str
    arguments: dict = Field(default_factory=dict)


class ToolPlan(BaseModel):
    calls: list[ToolCall] = Field(default_factory=list, max_length=10)


def _describe_args(spec: dict) -> str:
    """One line per tool: argument names, types, and which are required (*)."""
    schema = spec.get("inputSchema") or {}
    required = set(schema.get("required") or [])
    parts = []
    for name, prop in (schema.get("properties") or {}).items():
        kind = prop.get("type") or ("enum" if "enum" in prop else "any")
        parts.append(f"{name}{'*' if name in required else ''}: {kind}")
    return ", ".join(parts) or "none"


def _plan_prompt(question: str, customer_id: str, specs: list[dict]) -> str:
    lines = [f"Customer question:\n{question}", "",
             f"Customer id found in the question: {customer_id or 'none'}", "", "Tools:"]
    for spec in specs:
        lines += [f"- {spec['name']}: {spec.get('description', '')}", f"    arguments: {_describe_args(spec)}"]
    return "\n".join(lines)


def final_calls(planned: list[ToolCall], customer_id: str, question: str, limit: int) -> list[ToolCall]:
    """The calls to make: the customer lookup first when the question names a customer, no repeats."""
    calls = list(planned)
    if customer_id:
        calls.insert(0, ToolCall(tool="customer.build_context",
                                 arguments={"customer_id": customer_id, "question": question[:2000]}))
    unique, seen = [], set()
    for call in calls:
        key = (call.tool, json.dumps(call.arguments, sort_keys=True, default=str))
        if key not in seen:
            seen.add(key)
            unique.append(call)
    return unique[:limit]


def _evidence_texts(run: DraftRun, call: ToolCall, value) -> list[tuple[str, str]]:
    if call.tool == "customer.build_context" and isinstance(value, dict):
        run.context = run.context or value
        return [(e.kind, e.text) for e in build_evidence(value, run.now, run.sla)]
    body = json.dumps(value, default=str)
    if len(body) > RESULT_CHARS:
        body = body[:RESULT_CHARS] + "... (truncated)"
    return [(call.tool, f"{call.tool} {json.dumps(call.arguments, default=str)} returned {body}")]


def _front(run: DraftRun, limit: int):
    planned: list[ToolCall] = []

    async def plan(state: DraftState) -> dict:
        specs = [t for t in await run.mcp.list_tools() if t.get("name") in ALLOWED_TOOLS]
        prompt = run.redactor.redact(_plan_prompt(state["question"], state["customer_id"], specs))
        out, update = await ask_model(
            run, [SystemMessage(content=PLAN_SYSTEM), HumanMessage(content=prompt)], ToolPlan)
        if out is None:
            return update
        planned.extend(out.calls)
        return update

    async def run_tools(state: DraftState) -> dict:
        calls = final_calls(planned, state["customer_id"], state["question"], limit)
        if not calls:
            return stop(run, "no tool call was planned")
        allowed = []
        for call in calls:
            if call.tool in ALLOWED_TOOLS:
                allowed.append(call)
            else:
                run.errors.append(f"{call.tool}: tool_not_allowed")
        run.tool_calls += len(allowed)
        results = await asyncio.gather(*(
            run.mcp.call_tool(c.tool, c.arguments, deadline_ms=run.remaining_ms()) for c in allowed))
        texts: list[tuple[str, str]] = []
        for call, result in zip(allowed, results):
            if result.ok:
                texts += _evidence_texts(run, call, result.value)
            else:
                run.errors.append(f"{call.tool}: {result.error_code}")
        run.evidence = [Evidence(i, kind, text) for i, (kind, text) in enumerate(texts, start=1)]
        if not run.evidence:
            detail = f" ({'; '.join(run.errors)})" if run.errors else ""
            return stop(run, f"no tool call returned evidence{detail}")
        return {}

    return [("plan", plan), ("run_tools", run_tools)]


async def run_ask(mcp, models: list, question: str, now: datetime, max_llm_calls: int = 6,
                  timeout_s: float | None = None, sla: TenantSla | None = None,
                  max_tool_calls: int = MAX_TOOL_CALLS, trace: dict | None = None) -> DraftResult:
    run = DraftRun(mcp=mcp, models=models, now=now, max_llm_calls=max_llm_calls, timeout_s=timeout_s, sla=sla)
    return await execute(run, _front(run, max_tool_calls), extract_customer_id(question) or "", question, trace)
