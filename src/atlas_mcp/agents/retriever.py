"""Retriever agent — executes the plan by calling Atlas-MCP tools.

The retriever is the one agent that actually touches the outside world. It
uses a short tool-calling loop:

    while not done and iterations < cap:
        ask the LLM which tool to call with what arguments
        call the tool via AtlasMCPClient
        append the observation to the running conversation
    return findings

Bounded by ``max_iterations`` (default 6) so a confused LLM cannot spin
forever. On each iteration we also check the ATBA budget via the shared
orchestrator context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atlas_mcp.agents.base import Agent, AgentRun
from atlas_mcp.agents.mcp_client import AtlasMCPClient, ToolResult
from atlas_mcp.agents.planner import Plan
from atlas_mcp.agents.prompts import RETRIEVER_SYSTEM


# Tools the retriever is allowed to call. Read-only + composed/workflow
# retrievers. Enforced client-side as a second layer on top of the server's
# policy engine — belt and suspenders.
_ALLOWED_TOOLS = frozenset({
    "customer.build_context",
    "semantic_search",
    "hybrid_search",
    "postgres.query",
    "elasticsearch.search",
    "vector.search",
})


@dataclass
class Finding:
    source: str
    summary: str
    raw: Any = None


@dataclass
class RetrievalResult:
    findings: list[Finding] = field(default_factory=list)
    iterations_used: int = 0
    exceeded_budget: bool = False


class RetrieverAgent(Agent):
    name = "retriever"
    system_prompt = RETRIEVER_SYSTEM

    def __init__(self, llm, mcp_client: AtlasMCPClient, max_iterations: int = 6):
        super().__init__(llm)
        self.mcp = mcp_client
        self.max_iterations = max_iterations

    async def act(self, run: AgentRun, *, plan: Plan, question: str) -> RetrievalResult:  # type: ignore[override]
        tool_specs = await self._fetch_tool_specs()
        messages: list[dict] = [{
            "role": "user",
            "content": self._build_initial_prompt(question, plan, tool_specs),
        }]
        result = RetrievalResult()
        findings_accum: list[Finding] = []

        for i in range(1, self.max_iterations + 1):
            result.iterations_used = i
            decision = await self._complete_json(run, messages, max_tokens=800)

            if decision.get("done"):
                for f in decision.get("findings", []):
                    findings_accum.append(Finding(
                        source=str(f.get("source", "")),
                        summary=str(f.get("summary", "")),
                    ))
                break

            # Otherwise we expect a tool call instruction.
            tool_name = decision.get("tool")
            arguments = decision.get("arguments") or {}
            if not tool_name:
                messages.append({"role": "assistant", "content": json.dumps(decision)})
                messages.append({
                    "role": "user",
                    "content": "Your last response had no `tool` and no `done`. "
                               "Emit {\"done\": true, \"findings\": [...]} if you have enough, "
                               "otherwise {\"tool\": \"<name>\", \"arguments\": {...}}."
                })
                continue

            if tool_name not in _ALLOWED_TOOLS:
                observation = ToolResult(
                    ok=False, error_code="tool_not_allowed", retryable=False,
                    hint=f"{tool_name!r} is not in the retriever's allow-list",
                ).as_agent_observation()
            else:
                run.tool_calls += 1
                tool_result = await self.mcp.call_tool(tool_name, arguments)
                # Keep the raw result for synthesis; emit a compact view to the LLM.
                findings_accum.append(Finding(
                    source=tool_name,
                    summary=_summarise_result(tool_result, max_chars=500),
                    raw=tool_result.value,
                ))
                observation = tool_result.as_agent_observation()

            messages.append({"role": "assistant", "content": json.dumps(decision)})
            messages.append({
                "role": "user",
                "content": f"Tool `{tool_name}` result:\n{observation}\n\n"
                           f"Decide next step.",
            })
        else:
            result.exceeded_budget = True

        result.findings = findings_accum
        return result

    # ── Helpers ───────────────────────────────────────────────────────────
    async def _fetch_tool_specs(self) -> list[dict]:
        tools = await self.mcp.list_tools()
        return [t for t in tools if t.get("name") in _ALLOWED_TOOLS]

    @staticmethod
    def _build_initial_prompt(question: str, plan: Plan, tool_specs: list[dict]) -> str:
        lines = [
            f"Customer question: {question}",
            "",
            f"Customer id (if extracted): {plan.customer_id or '<unknown>'}",
            "",
            "Retrieval plan:",
        ]
        for step in plan.needs:
            lines.append(f"- [{step.id}] (p{step.priority}) {step.description}")
        lines.append("")
        lines.append("Available tools:")
        for t in tool_specs:
            lines.append(f"- {t['name']}: {t.get('description', '')}")
        lines.append("")
        lines.append(
            "Respond with EITHER "
            "{\"tool\": \"<name>\", \"arguments\": {...}} "
            "to call a tool, OR "
            "{\"done\": true, \"findings\": [{\"source\": \"<tool>\", \"summary\": \"...\"}]} "
            "when you have enough information."
        )
        return "\n".join(lines)


def _summarise_result(result: ToolResult, max_chars: int) -> str:
    text = result.as_agent_observation()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "… (truncated)"
