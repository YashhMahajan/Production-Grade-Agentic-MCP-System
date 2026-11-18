"""``ask``: one model call picks tools, code decides what runs, one model call writes the answer.

Driven by a scripted chat model and a fake MCP client: no network. Needs the ``copilot`` extra.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from scout_mcp.agents import cli  # noqa: E402
from scout_mcp.agents.graphs.ask import ALLOWED_TOOLS, ToolCall, final_calls, run_ask  # noqa: E402
from scout_mcp.agents.ledger import Ledger  # noqa: E402
from scout_mcp.agents.mcp_client import ToolResult  # noqa: E402
from scout_mcp.tools.registry import ToolRegistry  # noqa: E402
from tests.fakes import FakeMCP  # noqa: E402
from tests.fakes_lc import ScriptedChat  # noqa: E402

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
Q = "Why was the refund on o_9021 for CUST-1001 delayed?"
CONTEXT = {
    "customer_id": "CUST-1001", "question": Q,
    "profile": {"name": "Ana Reyes", "tier": "gold", "created_at": "2024-02-03T00:00:00Z"},
    "orders": [{"id": "o_9021", "status": "refund_pending", "total_cents": 4999, "currency": "USD",
                "created_at": "2026-11-01T00:00:00Z"}],
    "tickets": [], "docs": [],
}
DOCS = {"query": "refund", "results": [{"id": "doc_refunds", "score": 0.9, "title": "Refund policy"}]}


def plan(*calls) -> str:
    return json.dumps({"calls": [{"tool": t, "arguments": a} for t, a in calls]})


def answer(text="Order o_9021 totals 49.99 USD and is refund_pending.", cites=(2,)) -> str:
    return json.dumps({"claims": [{"text": text, "cites": list(cites)}], "needs_approval": True,
                       "insufficient": False})


GOOD = answer()
BAD = answer("Order o_9021 totals 59.99 USD and is refund_pending.")
ok = lambda value: ToolResult(ok=True, value=value)  # noqa: E731


class Err(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


async def go(replies, results, question=Q, cap=6, limit=4, mcp=None, **kw):
    mcp = mcp or FakeMCP(results)
    chat = ScriptedChat(replies=list(replies))
    out = await run_ask(mcp, [chat], question, NOW, max_llm_calls=cap, max_tool_calls=limit, **kw)
    return out, mcp, chat


# ── the happy path ─────────────────────────────────────────────────────────
async def test_a_question_with_a_customer_takes_two_model_calls_and_one_forced_tool_call():
    out, mcp, chat = await go([plan(), GOOD], [ok(CONTEXT)])
    assert out.outcome == "approved" and out.llm_calls == 2 and out.tool_calls == 1
    assert [c[0] for c in mcp.calls] == ["customer.build_context"]
    assert mcp.calls[0][1] == {"customer_id": "CUST-1001", "question": Q}
    assert "[S2]" in out.text and len(chat.seen) == 2


async def test_the_planner_is_told_the_tools_their_arguments_and_the_customer():
    _, _, chat = await go([plan(), GOOD], [ok(CONTEXT)])
    prompt = chat.seen[0][1]
    assert "Customer id found in the question: CUST-1001" in prompt
    assert "customer.build_context: Customer context." in prompt
    assert "customer_id*: string, question*: string, include_orders: boolean" in prompt
    assert "s3.put_object" not in prompt  # a tool off the allow-list is never offered


async def test_each_tool_result_becomes_numbered_evidence_in_plan_order():
    out, mcp, _ = await go(
        [plan(("semantic_search", {"query": "refund"})), answer("The refund policy covers this.", (3,))],
        [ok(CONTEXT), ok(DOCS)])
    assert [(e.id, e.kind) for e in out.evidence] == [(1, "profile"), (2, "order"), (3, "semantic_search")]
    assert out.evidence[-1].text.startswith('semantic_search {"query": "refund"} returned ')
    assert [c[0] for c in mcp.calls] == ["customer.build_context", "semantic_search"]
    assert out.outcome == "approved"


async def test_tool_calls_run_in_parallel():
    class Slow(FakeMCP):
        in_flight = peak = 0

        async def call_tool(self, name, arguments, deadline_ms=None):
            Slow.in_flight += 1
            Slow.peak = max(Slow.peak, Slow.in_flight)
            await asyncio.sleep(0.02)
            Slow.in_flight -= 1
            return await super().call_tool(name, arguments, deadline_ms)

    await go([plan(("semantic_search", {"query": "refund"})), GOOD], [ok(CONTEXT), ok(DOCS)],
             mcp=Slow([ok(CONTEXT), ok(DOCS)]))
    assert Slow.peak == 2


async def test_each_tool_call_carries_what_the_run_has_left():
    _, mcp, _ = await go([plan(), GOOD], [ok(CONTEXT)], timeout_s=30)
    assert 0 < mcp.calls[0][2] <= 30_000


# ── what the model may never be offered ────────────────────────────────────
async def test_no_tool_that_writes_is_on_the_allow_list():
    registry = ToolRegistry()
    await registry.discover()
    by_name = {t.meta.name: t.meta for t in registry}
    assert ALLOWED_TOOLS <= set(by_name), "the allow-list names a tool that does not exist"
    writers = {name for name, meta in by_name.items() if meta.destructive or not meta.read_only}
    assert {"s3.put_object", "ticket.escalate"} <= writers
    assert not writers & ALLOWED_TOOLS


# ── what code decides ──────────────────────────────────────────────────────
def test_the_customer_lookup_goes_first_and_repeats_are_dropped():
    planned = [ToolCall(tool="semantic_search", arguments={"query": "x"}),
               ToolCall(tool="semantic_search", arguments={"query": "x"}),
               ToolCall(tool="customer.build_context", arguments={"customer_id": "CUST-1001", "question": Q})]
    calls = final_calls(planned, "CUST-1001", Q, 4)
    assert [c.tool for c in calls] == ["customer.build_context", "semantic_search"]


def test_no_more_than_the_limit_is_ever_called():
    planned = [ToolCall(tool="semantic_search", arguments={"query": str(i)}) for i in range(9)]
    assert len(final_calls(planned, "", Q, 4)) == 4


async def test_a_tool_off_the_allow_list_is_refused_in_code_and_never_called():
    out, mcp, _ = await go([plan(("s3.put_object", {"key": "a", "content": "b"})), GOOD], [ok(CONTEXT)])
    assert [c[0] for c in mcp.calls] == ["customer.build_context"]
    assert out.errors == ["s3.put_object: tool_not_allowed"]


async def test_a_failed_tool_is_listed_and_is_never_evidence():
    out, _, _ = await go([plan(("semantic_search", {"query": "x"})), GOOD],
                         [ok(CONTEXT), ToolResult(ok=False, error_code="policy_denied")])
    assert out.errors == ["semantic_search: policy_denied"] and len(out.evidence) == 2


async def test_a_long_result_is_cut_and_a_number_past_the_cut_cannot_be_cited():
    big = {"rows": [{"n": i} for i in range(2000)]}
    wrong = answer("The count is 1999.", (3,))
    out, _, _ = await go([plan(("postgres.query", {"sql": "SELECT n FROM t"})), wrong, wrong],  # the revision fails too
                         [ok(CONTEXT), ok(big)])
    assert out.evidence[-1].text.endswith("... (truncated)")
    assert out.outcome == "flagged" and any("1999" in p for p in out.problems)


# ── no customer in the question ────────────────────────────────────────────
async def test_without_a_customer_the_model_chooses_every_tool():
    out, mcp, _ = await go([plan(("semantic_search", {"query": "refund policy"})),
                            answer("The refund policy covers this.", (1,))],
                           [ok(DOCS)], question="What is our refund policy?")
    assert out.outcome == "approved" and [c[0] for c in mcp.calls] == ["semantic_search"]


async def test_without_a_customer_and_without_a_plan_nothing_runs():
    out, mcp, chat = await go([plan()], [], question="What is our refund policy?")
    assert out.outcome == "evidence_pack" and out.reason == "no tool call was planned"
    assert mcp.calls == [] and len(chat.seen) == 1


# ── when something goes wrong ──────────────────────────────────────────────
async def test_if_every_tool_fails_the_run_ends_with_the_reasons():
    out, _, chat = await go([plan(), GOOD], [ToolResult(ok=False, error_code="policy_denied")])
    assert out.outcome == "evidence_pack" and "customer.build_context: policy_denied" in out.reason
    assert len(chat.seen) == 1  # the model was never asked to write from nothing


async def test_an_unreadable_plan_gets_one_counted_repair():
    out, _, chat = await go(["no idea", plan(), GOOD], [ok(CONTEXT)])
    assert out.outcome == "approved" and out.llm_calls == 3
    assert "not a single valid JSON" in chat.seen[1][-1]


async def test_a_plan_that_names_more_than_ten_calls_is_unreadable_not_truncated():
    too_many = plan(*[("semantic_search", {"query": str(i)}) for i in range(11)])
    out, _, _ = await go([too_many, too_many], [])
    assert out.outcome == "evidence_pack" and "unreadable" in out.reason


async def test_a_wrong_number_gets_one_revision_and_the_total_stays_inside_the_cap():
    out, _, _ = await go([plan(), BAD, GOOD], [ok(CONTEXT)], cap=3)
    assert out.outcome == "approved" and out.llm_calls == 3


async def test_the_cap_leaves_the_tools_run_and_the_answer_unwritten():
    out, mcp, _ = await go([plan(), GOOD], [ok(CONTEXT)], cap=1)
    assert out.outcome == "evidence_pack" and out.reason == "call_budget" and len(mcp.calls) == 1
    assert out.context == CONTEXT  # so the caller can still show the brief


async def test_a_model_that_cannot_be_used_ends_the_run_after_one_attempt():
    chat = ScriptedChat(replies=[Err(401)])
    mcp = FakeMCP([])
    out = await run_ask(mcp, [chat], Q, NOW)
    assert out.outcome == "evidence_pack" and "FinalLLMError" in out.reason and mcp.calls == []


async def test_contact_details_in_the_question_never_reach_the_model():
    _, _, chat = await go([plan(), GOOD], [ok(CONTEXT)],
                          question=f"{Q} Write to ana@example.com or call 415-555-0133.")
    sent = json.dumps(chat.seen)
    assert "ana@example.com" not in sent and "415-555-0133" not in sent


# ── the command ────────────────────────────────────────────────────────────
def ledger(tmp_path, limit=40):
    return Ledger(tmp_path / "ledger.json", limit=limit, today=lambda: "d")


async def test_the_ask_command_prints_the_answer_and_its_evidence_and_charges_the_ledger(tmp_path):
    led = ledger(tmp_path)
    mcp = FakeMCP([ok(CONTEXT)])
    text, code = await cli.ask_command(mcp, Q, led, NOW, models=[ScriptedChat(replies=[plan(), GOOD])])
    assert code == 0 and "DRAFT REPLY (approved)" in text and "[S2] Order o_9021" in text
    assert "model calls: 2, tool calls: 1" in text and led.remaining() == 38


async def test_the_ask_command_names_tool_calls_that_returned_nothing(tmp_path):
    mcp = FakeMCP([ok(CONTEXT)])
    models = [ScriptedChat(replies=[plan(("s3.put_object", {"key": "a", "content": "b"})), GOOD])]
    text, _ = await cli.ask_command(mcp, Q, ledger(tmp_path), NOW, models=models)
    assert "returned no evidence: s3.put_object: tool_not_allowed" in text


async def test_an_exhausted_ledger_gives_the_brief_and_exit_3_without_a_model(tmp_path):
    mcp = FakeMCP([ok(CONTEXT)])
    text, code = await cli.ask_command(mcp, Q, ledger(tmp_path, limit=0), NOW)
    assert code == 3 and "Ana Reyes" in text


async def test_no_key_gives_the_brief_and_exit_3(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOUT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    text, code = await cli.ask_command(FakeMCP([ok(CONTEXT)]), Q, ledger(tmp_path), NOW)
    assert code == 3 and "Ana Reyes" in text


async def test_a_question_with_no_customer_and_no_model_says_how_to_get_a_brief(tmp_path):
    text, code = await cli.ask_command(FakeMCP([]), "What is our refund policy?", ledger(tmp_path, limit=0), NOW)
    assert code == 3 and "Include a customer id" in text


async def test_a_model_failure_gives_the_brief_from_the_run_and_still_charges_the_attempt(tmp_path):
    led = ledger(tmp_path)
    text, code = await cli.ask_command(FakeMCP([ok(CONTEXT)]), Q, led, NOW,
                                       models=[ScriptedChat(replies=[plan(), Err(402)])])
    assert code == 3 and "Ana Reyes" in text and led.remaining() == 38


async def test_tools_that_cannot_be_reached_are_reported_not_raised(tmp_path):
    class Down(FakeMCP):
        async def call_tool(self, name, arguments, deadline_ms=None):
            raise httpx.ConnectError("refused")

    text, code = await cli.ask_command(Down(), Q, ledger(tmp_path), NOW,
                                       models=[ScriptedChat(replies=[plan(), GOOD])])
    assert code == 1 and "ConnectError" in text
