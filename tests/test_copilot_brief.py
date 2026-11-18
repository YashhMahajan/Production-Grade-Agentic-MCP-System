"""The no-model parts of the copilot: brief, redaction, the daily ledger, exit codes."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone

import pytest

from scout_mcp.agents import cli
from scout_mcp.agents.brief import make_activity, make_brief
from scout_mcp.agents.ledger import Ledger, LedgerExhausted
from scout_mcp.agents.mcp_client import ToolResult
from scout_mcp.agents.redact import Redactor
from scout_mcp.agents.sla import Sla
from tests.fakes import FakeMCP

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
SLA = Sla({"default": {"high": 4, "normal": 24, "low": 72}, "tiers": {"gold": {"high": 2, "low": 48}}}).for_tenant("acme")
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
    flags = make_brief(CONTEXT, NOW, SLA).flags
    assert "open high-priority ticket TCK-1" in flags
    assert "ticket TCK-1: SLA target 2h, breached (147h open)" in flags
    assert "incomplete: docs (timeout)" in flags
    assert not any("TCK-2" in f for f in flags)  # a low ticket opened 4h ago, against 48h for gold


def test_without_an_sla_a_ticket_is_never_flagged_for_its_age():
    assert not any("SLA" in f for f in make_brief(CONTEXT, NOW).flags)


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


def test_a_free_model_is_refused_in_production_without_an_opt_in(monkeypatch):
    monkeypatch.setenv("SCOUT_ENV", "production")
    monkeypatch.delenv("SCOUT_ALLOW_FREE_LLM", raising=False)
    assert "free" in cli.free_model_problem(["a/b:free"])
    monkeypatch.setenv("SCOUT_ALLOW_FREE_LLM", "1")
    assert cli.free_model_problem(["a/b:free"]) is None
    monkeypatch.setenv("SCOUT_ENV", "development")
    monkeypatch.delenv("SCOUT_ALLOW_FREE_LLM")
    assert cli.free_model_problem(["a/b:free"]) is None


# ── activity ───────────────────────────────────────────────────────────────
ACTIVITY = {"customer_id": "CUST-1001", "since": "2026-11-09T12:00:00+00:00",
            "orders": [{"id": "o_2", "status": "shipped", "total_cents": 1250, "currency": "USD",
                        "created_at": "2026-11-12T00:00:00Z"}],
            "tickets": [], "partial_errors": {"tickets": "elasticsearch_error"}}


def test_activity_lists_what_is_new_and_what_could_not_be_read():
    text = make_activity(ACTIVITY)
    assert "since 2026-11-09" in text and "o_2  shipped  12.50 USD  2026-11-12" in text
    assert "Tickets\n  none" in text and "incomplete: tickets (elasticsearch_error)" in text


def test_a_relative_or_an_absolute_start_is_read():
    assert cli.parse_since("7d", NOW) == datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    assert cli.parse_since("2026-11-10", NOW) == datetime(2026, 11, 10, tzinfo=timezone.utc)
    assert cli.parse_since("2026-11-10T08:00:00Z", NOW).hour == 8
    with pytest.raises(ValueError):
        cli.parse_since("last week", NOW)


async def test_the_activity_command_makes_one_tool_call_and_no_model_call():
    mcp = FakeMCP([ToolResult(ok=True, value=ACTIVITY)])
    text, code = await cli.activity_command(mcp, "CUST-1001", cli.parse_since("7d", NOW))
    assert code == 0 and "o_2" in text
    assert mcp.calls[0][:2] == ("customer.activity_since",
                                {"customer_id": "CUST-1001", "since": "2026-11-09T12:00:00+00:00"})


async def test_the_activity_command_reports_a_failed_lookup():
    mcp = FakeMCP([ToolResult(ok=False, error_code="policy_denied")])
    text, code = await cli.activity_command(mcp, "CUST-1001", NOW)
    assert code == 1 and "policy_denied" in text
