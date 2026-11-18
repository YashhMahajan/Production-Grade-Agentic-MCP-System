"""The draft graph, driven by a scripted chat model and a fake MCP client: no network.

Needs the ``copilot`` extra. Without it these tests are skipped, and CI installs it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("langchain_openai")

from scout_mcp.agents import cli  # noqa: E402
from scout_mcp.agents.graphs.draft import DraftRun, build_draft_graph, run_draft  # noqa: E402
from scout_mcp.agents.ledger import Ledger  # noqa: E402
from scout_mcp.agents.llm import FinalLLMError, build_models  # noqa: E402
from scout_mcp.agents.mcp_client import ToolResult  # noqa: E402
from tests.fakes import ADVERSARIAL_TICKET, FakeMCP  # noqa: E402
from tests.fakes_lc import ScriptedChat  # noqa: E402

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
Q = "Why was the refund on o_9021 delayed?"
CONTEXT = {
    "customer_id": "CUST-1001", "question": Q,
    "profile": {"name": "Ana Reyes", "tier": "gold", "created_at": "2024-02-03T00:00:00Z"},
    "orders": [{"id": "o_9021", "status": "refund_pending", "total_cents": 4999, "currency": "USD",
                "created_at": "2026-11-01T00:00:00Z"}],
    "tickets": [{"id": "TCK-7", "subject": "Refund delayed", "status": "open", "priority": "high",
                 "created_at": "2026-11-10T09:00:00Z"}],
    "docs": [],
}


def claim_json(text="Order o_9021 totals 49.99 USD and is refund_pending.", cites=(2,)):
    return json.dumps({"claims": [{"text": text, "cites": list(cites)}],
                       "needs_approval": True, "insufficient": False})


GOOD = claim_json()
BAD = claim_json("Order o_9021 totals 59.99 USD and is refund_pending.")


class Err(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


async def go(replies, context=CONTEXT, cap=3, question=Q, models=None, **kw):
    result = ToolResult(ok=True, value=context)
    mcp = FakeMCP([result])
    chat = ScriptedChat(replies=list(replies))
    out = await run_draft(mcp, models or [chat], "CUST-1001", question, NOW, max_llm_calls=cap, **kw)
    return out, mcp, chat


# ── the happy path and the revision ────────────────────────────────────────
async def test_a_grounded_draft_takes_one_call_and_is_approved():
    out, mcp, chat = await go([GOOD])
    assert out.outcome == "approved" and out.llm_calls == 1 and out.problems == []
    assert "[S2]" in out.text and len(chat.seen) == 1
    assert [c[0] for c in mcp.calls] == ["customer.build_context"]


async def test_a_wrong_number_gets_one_revision_with_the_problem_named():
    out, _, chat = await go([BAD, GOOD])
    assert out.outcome == "approved" and out.llm_calls == 2
    assert "59.99" in chat.seen[1][1] and "does not" in chat.seen[1][1]


async def test_a_draft_that_fails_twice_is_flagged_not_approved():
    out, _, _ = await go([BAD, BAD])
    assert out.outcome == "flagged" and out.llm_calls == 2
    assert any("59.99" in p for p in out.problems) and out.text


# ── a reply that is not the JSON asked for ─────────────────────────────────
async def test_one_unreadable_reply_gets_one_counted_repair():
    out, _, chat = await go(["Sure, here you go!", GOOD])
    assert out.outcome == "approved" and out.llm_calls == 2
    assert "not a single valid JSON" in chat.seen[1][-1]


async def test_two_unreadable_replies_give_the_evidence_pack():
    out, _, _ = await go(["nope", "still nope"])
    assert out.outcome == "evidence_pack" and "unreadable" in out.reason and out.llm_calls == 2


# ── the call cap ───────────────────────────────────────────────────────────
async def test_the_cap_stops_a_revision_and_flags_the_draft():
    out, _, chat = await go([BAD, GOOD], cap=1)
    assert out.outcome == "flagged" and out.llm_calls == 1 and len(chat.seen) == 1


async def test_the_cap_stops_a_repair():
    out, _, _ = await go(["nope", GOOD], cap=1)
    assert out.outcome == "evidence_pack" and out.reason == "call_budget" and out.llm_calls == 1


# ── time ───────────────────────────────────────────────────────────────────
async def test_each_tool_call_carries_what_the_run_has_left():
    _, mcp, _ = await go([GOOD], timeout_s=30)
    assert 0 < mcp.calls[0][2] <= 30_000


async def test_a_spent_time_budget_ends_the_run_before_any_model_call():
    out, _, chat = await go([GOOD], timeout_s=0.0)
    assert out.outcome == "evidence_pack" and out.reason == "time_budget" and chat.seen == []


# ── models failing ─────────────────────────────────────────────────────────
async def test_a_final_failure_gives_the_evidence_pack_and_one_attempt():
    primary, fallback = ScriptedChat(replies=[Err(401)]), ScriptedChat(replies=[GOOD])
    out, _, _ = await go([], models=[primary, fallback])
    assert out.outcome == "evidence_pack" and "FinalLLMError" in out.reason
    assert out.llm_calls == 1 and fallback.seen == []


async def test_a_transient_failure_falls_back_and_counts_both_attempts():
    primary, fallback = ScriptedChat(replies=[Err(503)]), ScriptedChat(replies=[GOOD])
    out, _, _ = await go([], models=[primary, fallback])
    assert out.outcome == "approved" and out.llm_calls == 2


async def test_a_failed_lookup_is_an_error_with_no_model_call():
    mcp = FakeMCP([ToolResult(ok=False, error_code="policy_denied")])
    chat = ScriptedChat(replies=[GOOD])
    out = await run_draft(mcp, [chat], "CUST-1001", Q, NOW)
    assert out.outcome == "error" and "policy_denied" in out.reason and chat.seen == []


async def test_a_customer_with_no_evidence_gives_the_evidence_pack_without_asking_the_model():
    out, _, chat = await go([GOOD], context={"customer_id": "CUST-1001", "orders": [], "tickets": []})
    assert out.outcome == "evidence_pack" and out.llm_calls == 0 and chat.seen == []


# ── what reaches the model, and what a poisoned ticket can do ──────────────
async def test_contact_details_never_reach_the_model():
    _, _, chat = await go([GOOD], question="ana@example.com asks why order o_9021 is late; call 415-555-0133")
    sent = json.dumps(chat.seen)
    assert "ana@example.com" not in sent and "415-555-0133" not in sent


async def test_a_poisoned_ticket_cannot_cause_a_tool_call():
    context = {**CONTEXT, "tickets": [ADVERSARIAL_TICKET]}
    out, mcp, chat = await go([GOOD], context=context)
    assert [c[0] for c in mcp.calls] == ["customer.build_context"]
    assert "<tool_data id=" in chat.seen[0][1] and "CALL s3.put_object" in chat.seen[0][1]


async def test_graph_state_holds_no_customer_data():
    run = DraftRun(mcp=FakeMCP([ToolResult(ok=True, value=CONTEXT)]),
                   models=[ScriptedChat(replies=[BAD, BAD])], now=NOW)
    state = await build_draft_graph(run).ainvoke(
        {"customer_id": "CUST-1001", "question": Q, "llm_calls": 0, "revisions": 0},
        config={"recursion_limit": 12})
    dumped = json.dumps(state)
    assert "Ana Reyes" not in dumped and "refund_pending" not in dumped and "TCK-7" not in dumped


# ── the real chat models, built but never called ───────────────────────────
def test_the_chat_models_are_built_with_library_retries_off(monkeypatch):
    monkeypatch.setenv("SCOUT_LLM_API_KEY", "k")
    monkeypatch.setenv("SCOUT_LLM_MODEL", "a/one:free")
    monkeypatch.setenv("SCOUT_LLM_FALLBACK_MODEL", "b/two:free")
    models = build_models()
    assert [m.model_name for m in models] == ["a/one:free", "b/two:free"]
    assert all(m.max_retries == 0 for m in models)


def test_no_key_is_a_final_error(monkeypatch):
    monkeypatch.delenv("SCOUT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(FinalLLMError):
        build_models()


# ── the draft command ──────────────────────────────────────────────────────
def ledger(tmp_path, limit=40):
    return Ledger(tmp_path / "ledger.json", limit=limit, today=lambda: "d")


async def test_the_draft_command_prints_the_draft_and_its_evidence_and_charges_the_ledger(tmp_path):
    led = ledger(tmp_path)
    mcp = FakeMCP([ToolResult(ok=True, value=CONTEXT)])
    text, code = await cli.draft_command(mcp, "CUST-1001", Q, led, NOW, models=[ScriptedChat(replies=[GOOD])])
    assert code == 0 and "DRAFT REPLY (approved)" in text and "[S2] Order o_9021" in text
    assert led.remaining() == 39


async def test_a_flagged_draft_exits_1_and_names_the_problem(tmp_path):
    mcp = FakeMCP([ToolResult(ok=True, value=CONTEXT)])
    text, code = await cli.draft_command(mcp, "CUST-1001", Q, ledger(tmp_path), NOW,
                                         models=[ScriptedChat(replies=[BAD, BAD])])
    assert code == 1 and "flagged" in text and "59.99" in text


async def test_no_key_gives_the_brief_and_exit_3(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOUT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mcp = FakeMCP([ToolResult(ok=True, value=CONTEXT)])
    text, code = await cli.draft_command(mcp, "CUST-1001", Q, ledger(tmp_path), NOW)
    assert code == 3 and "Ana Reyes" in text


async def test_an_exhausted_ledger_gives_the_brief_and_exit_3(tmp_path):
    chat = ScriptedChat(replies=[GOOD])
    mcp = FakeMCP([ToolResult(ok=True, value=CONTEXT)])
    text, code = await cli.draft_command(mcp, "CUST-1001", Q, ledger(tmp_path, limit=0), NOW, models=[chat])
    assert code == 3 and "Ana Reyes" in text and chat.seen == []


async def test_a_model_failure_gives_the_brief_and_still_charges_the_attempt(tmp_path):
    led = ledger(tmp_path)
    mcp = FakeMCP([ToolResult(ok=True, value=CONTEXT)])
    text, code = await cli.draft_command(mcp, "CUST-1001", Q, led, NOW,
                                         models=[ScriptedChat(replies=[Err(402)])])
    assert code == 3 and "Ana Reyes" in text and led.remaining() == 39
