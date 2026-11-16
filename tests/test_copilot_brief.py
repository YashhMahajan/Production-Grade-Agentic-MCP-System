"""The no-model parts of the copilot: brief, redaction, the daily ledger, exit codes."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone

import pytest

from scout_mcp.agents import cli
from scout_mcp.agents.brief import make_brief
from scout_mcp.agents.ledger import Ledger, LedgerExhausted
from scout_mcp.agents.mcp_client import ToolResult
from scout_mcp.agents.orchestrator import SupportCopilot
from scout_mcp.agents.redact import RedactingLLM, Redactor
from tests.fakes import FakeMCP, ScriptedLLM

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
CONTEXT = {
    "customer_id": "CUST-1001", "question": "brief",
    "profile": {"id": "CUST-1001", "name": "Ana Reyes", "email": "ana@example.com", "tier": "gold"},
    "orders": [{"id": "o_1", "status": "refund_pending", "total_cents": 4999, "currency": "USD",
                "created_at": "2026-11-01T00:00:00Z"}],
    "tickets": [{"id": "TCK-1", "subject": "Refund delayed", "status": "open", "priority": "high",
                 "created_at": "2026-11-10T09:00:00Z"},
                {"id": "TCK-2", "subject": "Login", "status": "pending", "priority": "low",
                 "created_at": "2026-11-16T08:00:00Z"}],
    "docs": [], "partial_errors": {"docs": "timeout"},
}


# ── brief ──────────────────────────────────────────────────────────────────
def test_a_brief_lists_the_customer_orders_and_tickets_with_their_age():
    brief = make_brief(CONTEXT, NOW)
    assert "Ana Reyes" in brief.text and "gold" in brief.text
    assert "o_1" in brief.text and "49.99 USD" in brief.text
    assert "TCK-1" in brief.text and "6 days" in brief.text


def test_a_brief_raises_fixed_rule_flags():
    flags = make_brief(CONTEXT, NOW).flags
    assert "open high-priority ticket TCK-1" in flags
    assert "ticket TCK-1 is 6 days old" in flags
    assert "incomplete: docs (timeout)" in flags


def test_a_quiet_customer_has_no_flags():
    quiet = {"customer_id": "C", "profile": {"name": "B"}, "orders": [], "tickets": []}
    assert make_brief(quiet, NOW).flags == []


# ── redaction ──────────────────────────────────────────────────────────────
def test_the_redactor_hides_emails_phones_and_card_numbers_and_restores_them():
    r = Redactor()
    text = "Mail ana@example.com or call +1 415-555-0133, card 4111 1111 1111 1111."
    hidden = r.redact(text)
    for secret in ("ana@example.com", "415-555-0133", "4111 1111 1111 1111"):
        assert secret not in hidden
    assert r.restore(hidden) == text


def test_the_same_value_gets_the_same_placeholder():
    r = Redactor()
    assert r.redact("a@b.co and a@b.co") == "<email_1> and <email_1>"


async def test_no_prompt_that_reaches_the_model_holds_customer_pii():
    inner = ScriptedLLM(["Reply to <email_1> now."])
    llm = RedactingLLM(inner)
    out = await llm.complete("sys ana@example.com", [{"role": "user", "content": "ana@example.com 4111111111111111"}])
    sent = json.dumps(inner.calls)
    assert "ana@example.com" not in sent and "4111111111111111" not in sent
    assert out["choices"][0]["message"]["content"] == "Reply to ana@example.com now."


# ── daily ledger ───────────────────────────────────────────────────────────
def test_the_ledger_stops_at_its_limit_and_starts_over_the_next_day(tmp_path):
    day = ["2026-11-16"]
    ledger = Ledger(tmp_path / "ledger.json", limit=3, today=lambda: day[0])
    ledger.spend(2)
    assert ledger.remaining() == 1
    with pytest.raises(LedgerExhausted):
        ledger.spend(2)
    day[0] = "2026-11-17"
    assert ledger.remaining() == 3


def test_the_ledger_file_is_private(tmp_path):
    path = tmp_path / "sub" / "ledger.json"
    Ledger(path, limit=5, today=lambda: "d").spend(1)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ── commands ───────────────────────────────────────────────────────────────
async def test_brief_makes_no_model_call_and_needs_no_key():
    mcp = FakeMCP([ToolResult(ok=True, value=CONTEXT)])
    text, code = await cli.brief_command(mcp, "CUST-1001", NOW)
    assert code == 0 and "Ana Reyes" in text
    assert mcp.calls[0][0] == "customer.build_context"


async def test_brief_reports_a_failed_lookup():
    mcp = FakeMCP([ToolResult(ok=False, error_code="policy_denied")])
    text, code = await cli.brief_command(mcp, "CUST-1001", NOW)
    assert code == 1 and "policy_denied" in text


async def test_an_exhausted_ledger_gives_the_evidence_pack_and_exit_3(tmp_path):
    ledger = Ledger(tmp_path / "l.json", limit=0, today=lambda: "d")
    llm = ScriptedLLM([])
    mcp = FakeMCP([ToolResult(ok=True, value=CONTEXT)])
    text, code = await cli.ask_command(SupportCopilot(mcp, llm=llm), mcp, "Why CUST-1001?", ledger, NOW)
    assert code == 3 and "Ana Reyes" in text and llm.calls == []


async def test_a_model_run_charges_the_ledger_for_its_calls(tmp_path):
    plan = json.dumps({"needs": [], "customer_id_required": True, "notes": ""})
    replies = [plan, json.dumps({"tool": "customer.build_context",
                                 "arguments": {"customer_id": "CUST-1001", "question": "q"}}),
               json.dumps({"done": True}), "Refund is pending [S1].",
               json.dumps({"verdict": "approve", "issues": []})]
    ledger = Ledger(tmp_path / "l.json", limit=40, today=lambda: "d")
    mcp = FakeMCP([ToolResult(ok=True, value=CONTEXT)])
    copilot = SupportCopilot(mcp, llm=ScriptedLLM(replies))
    text, code = await cli.ask_command(copilot, mcp, "Why CUST-1001?", ledger, NOW)
    assert code == 0 and ledger.remaining() == 35


def test_a_free_model_is_refused_in_production_without_an_opt_in(monkeypatch):
    monkeypatch.setenv("SCOUT_ENV", "production")
    monkeypatch.delenv("SCOUT_ALLOW_FREE_LLM", raising=False)
    assert "free" in cli.free_model_problem(["a/b:free"])
    monkeypatch.setenv("SCOUT_ALLOW_FREE_LLM", "1")
    assert cli.free_model_problem(["a/b:free"]) is None
    monkeypatch.setenv("SCOUT_ENV", "development")
    monkeypatch.delenv("SCOUT_ALLOW_FREE_LLM")
    assert cli.free_model_problem(["a/b:free"]) is None
