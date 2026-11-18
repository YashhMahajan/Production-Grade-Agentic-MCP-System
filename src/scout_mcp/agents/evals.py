"""Replay evaluation of the draft flow.

A case holds what ``customer.build_context`` returned, the model replies to hand back in order, and what the
checks must conclude. Each case runs through the real graph (``run_draft``) with a chat model that replays
those replies, so no model is called and the result is the same every time.

The cases are written by hand, so this measures the graph and its checks on known replies: that a wrong
total, a promise, a missing citation, an invented email or an over-long draft is caught, and that a sound
draft is not. It says nothing about how well any model writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from scout_mcp.agents.mcp_client import ToolResult
from scout_mcp.agents.sla import Sla

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
SLA = Sla({"default": {"high": 4, "normal": 24, "low": 72}, "tiers": {"gold": {"high": 2}}}).for_tenant("acme")
# Relative to where the script or the tests run: the repository root.
CASES = Path("tests/replays/draft_cases.json")
DATASET = "scout-copilot-replays"

# Which kind of problem a line from the checks is about.
_KINDS = (("citation", "cites"), ("number", "states "), ("contact", "contact detail"),
          ("commitment", "commits"), ("length", "words, over"), ("empty", "makes no claims"))


def categories(problems: list[str]) -> list[str]:
    return sorted({kind for kind, marker in _KINDS if any(marker in p for p in problems)})


class ReplayMCP:
    """Hands back the case's customer context for the one tool call a draft makes."""

    def __init__(self, context: dict):
        self.context, self.calls = context, []

    async def call_tool(self, name, arguments, deadline_ms=None) -> ToolResult:
        self.calls.append(name)
        return ToolResult(ok=True, value=self.context)


@dataclass
class CaseResult:
    name: str
    outcome: str
    categories: list[str]
    llm_calls: int
    tool_calls: int
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def load_cases(path: Path = CASES) -> list[dict]:
    return json.loads(path.read_text())


async def run_case(case: dict) -> CaseResult:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from scout_mcp.agents.graphs.draft import run_draft

    replies = [r if isinstance(r, str) else json.dumps(r) for r in case["replies"]]
    mcp = ReplayMCP(case["context"])
    out = await run_draft(mcp, [FakeListChatModel(responses=replies)], "CUST-1001", case["question"], NOW,
                          max_llm_calls=3, sla=SLA)
    got = CaseResult(case["name"], out.outcome, categories(out.problems), out.llm_calls, len(mcp.calls))
    want = case["expect"]
    for key, actual in (("outcome", got.outcome), ("categories", got.categories),
                        ("llm_calls", got.llm_calls), ("tool_calls", got.tool_calls)):
        if key in want and want[key] != actual:
            got.failures.append(f"{key}: expected {want[key]!r}, got {actual!r}")
    return got


async def run_all(cases: list[dict] | None = None) -> list[CaseResult]:
    return [await run_case(c) for c in (cases if cases is not None else load_cases())]


def summary(results: list[CaseResult]) -> dict:
    passed = sum(r.passed for r in results)
    outcomes: dict[str, int] = {}
    for r in results:
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
    return {"cases": len(results), "agreed": passed, "agreement": round(passed / len(results), 4) if results else None,
            "outcomes": outcomes, "failures": {r.name: r.failures for r in results if r.failures}}


def upload_dataset(client, cases: list[dict], name: str = DATASET) -> str:
    """Put the cases in LangSmith as a dataset, so the same inputs can be run and compared there.

    The cases are hand-written and hold no customer data. An existing dataset is left alone.
    """
    if client.has_dataset(dataset_name=name):
        return f"dataset {name} already exists; left as it is"
    dataset = client.create_dataset(dataset_name=name, description="Replay cases for the scout-copilot draft flow")
    client.create_examples(dataset_id=dataset.id, examples=[
        {"inputs": {"question": c["question"], "context": c["context"], "replies": c["replies"]},
         "outputs": c["expect"], "metadata": {"name": c["name"]}} for c in cases])
    return f"created dataset {name} with {len(cases)} examples"
