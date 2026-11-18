"""``escalate``: the graph pauses for a person, then asks the server, which holds the call for a reviewer.

A fake MCP client and no model: nothing here calls a network. Needs the ``copilot`` extra.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("langgraph")

from scout_mcp.agents import cli  # noqa: E402
from scout_mcp.agents.graphs.escalate import run_escalate  # noqa: E402
from scout_mcp.agents.mcp_client import ToolResult  # noqa: E402
from tests.fakes import FakeMCP  # noqa: E402

REASON = "Past its SLA and the customer has written twice."
FOUND = ToolResult(ok=True, value={"total": 1, "took_ms": 1, "hits": [
    {"id": "t_5002", "score": 1.0,
     "source": {"subject": "Where is order o_9003?", "status": "pending", "priority": "normal",
                "customer_id": "CUST-1002"}}]})
NOT_FOUND = ToolResult(ok=True, value={"total": 0, "took_ms": 1, "hits": []})
HELD = ToolResult(ok=False, error_code="pending_approval", hint="held", context={"approval_id": "ap_42"})


class Journal(FakeMCP):
    """A FakeMCP that also logs when the person is asked, so the order of events can be checked."""

    def __init__(self, results):
        super().__init__(results)
        self.events: list[str] = []

    async def call_tool(self, name, arguments, deadline_ms=None):
        self.events.append(name)
        return await super().call_tool(name, arguments, deadline_ms)


def answering(answer: bool, mcp: Journal):
    asked: list[str] = []

    async def confirm(proposal: str) -> bool:
        asked.append(proposal)
        mcp.events.append("confirm")
        return answer
    return confirm, asked


# ── the graph ──────────────────────────────────────────────────────────────
async def test_a_confirmed_request_is_sent_and_held_for_a_reviewer():
    mcp = Journal([FOUND, HELD])
    confirm, asked = answering(True, mcp)
    out = await run_escalate(mcp, "t_5002", REASON, confirm)
    assert out.outcome == "pending_approval" and out.approval_id == "ap_42"
    assert mcp.calls[1][:2] == ("ticket.escalate", {"ticket_id": "t_5002", "reason": REASON})
    assert len(asked) == 1


async def test_the_person_is_shown_the_ticket_and_the_reason_before_anything_is_sent():
    mcp = Journal([FOUND, HELD])
    confirm, asked = answering(True, mcp)
    await run_escalate(mcp, "t_5002", REASON, confirm)
    assert mcp.events == ["elasticsearch.search", "confirm", "ticket.escalate"]
    assert "t_5002" in asked[0] and "Where is order o_9003?" in asked[0] and "CUST-1002" in asked[0]
    assert REASON in asked[0]


async def test_the_lookup_is_by_id_inside_the_callers_tenant_search():
    mcp = Journal([FOUND, HELD])
    await run_escalate(mcp, "t_5002", REASON, answering(True, mcp)[0])
    name, args, _ = mcp.calls[0]
    assert name == "elasticsearch.search" and args["index"] == "tickets"
    assert args["query"] == {"ids": {"values": ["t_5002"]}}


async def test_a_declined_request_sends_nothing():
    mcp = Journal([FOUND])
    out = await run_escalate(mcp, "t_5002", REASON, answering(False, mcp)[0])
    assert out.outcome == "cancelled" and [c[0] for c in mcp.calls] == ["elasticsearch.search"]


async def test_a_ticket_that_does_not_exist_is_never_proposed():
    mcp = Journal([NOT_FOUND])
    confirm, asked = answering(True, mcp)
    out = await run_escalate(mcp, "t_9999", REASON, confirm)
    assert out.outcome == "not_found" and "no ticket t_9999" in out.detail
    assert asked == [] and [c[0] for c in mcp.calls] == ["elasticsearch.search"]


async def test_a_lookup_that_fails_ends_the_run():
    mcp = Journal([ToolResult(ok=False, error_code="policy_denied")])
    confirm, asked = answering(True, mcp)
    out = await run_escalate(mcp, "t_5002", REASON, confirm)
    assert out.outcome == "error" and "policy_denied" in out.detail and asked == []


async def test_after_a_reviewer_approves_the_approved_call_is_made_without_asking_again():
    done = ToolResult(ok=True, value={"ticket_id": "t_5002", "priority": "high", "escalated_at": "2026-11-18T10:00:00Z"})
    mcp = Journal([FOUND, done])
    confirm, asked = answering(False, mcp)  # would refuse, if it were asked
    out = await run_escalate(mcp, "t_5002", REASON, confirm, approval_id="ap_42")
    assert out.outcome == "done" and "now high priority" in out.detail and asked == []
    assert mcp.calls[1][:2] == ("ticket.escalate", {"ticket_id": "t_5002", "reason": REASON, "approval_id": "ap_42"})


async def test_a_refusal_from_the_server_is_reported_with_its_reason():
    refused = ToolResult(ok=False, error_code="approval_already_used", hint="ask for a new one")
    mcp = Journal([FOUND, refused])
    out = await run_escalate(mcp, "t_5002", REASON, answering(True, mcp)[0], approval_id="ap_42")
    assert out.outcome == "error" and out.detail == "approval_already_used: ask for a new one"


# ── the command ────────────────────────────────────────────────────────────
async def test_a_sent_request_says_who_has_to_approve_and_what_to_run_next():
    mcp = Journal([FOUND, HELD])
    text, code = await cli.escalate_command(mcp, "t_5002", REASON, answering(True, mcp)[0])
    assert code == 0 and "ap_42" in text and "the copilot cannot" in text
    assert f'scout-copilot escalate t_5002 "{REASON}" --approval ap_42' in text


async def test_the_command_exits_1_when_nothing_was_sent():
    mcp = Journal([FOUND])
    text, code = await cli.escalate_command(mcp, "t_5002", REASON, answering(False, mcp)[0])
    assert code == 1 and "nothing was sent" in text


async def test_tools_that_cannot_be_reached_are_reported_not_raised():
    class Down(FakeMCP):
        async def call_tool(self, name, arguments, deadline_ms=None):
            raise httpx.ConnectError("refused")

    text, code = await cli.escalate_command(Down(), "t_5002", REASON, cli._confirm_without_asking)
    assert code == 1 and "ConnectError" in text


@pytest.mark.parametrize("typed, expected", [("y", True), ("YES", True), (" yes ", True), ("", False), ("n", False),
                                             ("sure", False)])
async def test_only_a_yes_at_the_terminal_confirms(monkeypatch, capsys, typed, expected):
    monkeypatch.setattr("builtins.input", lambda prompt="": typed)
    assert await cli._confirm_at_the_terminal("the proposal") is expected
    assert "the proposal" in capsys.readouterr().out


async def test_no_terminal_means_no(monkeypatch):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert await cli._confirm_at_the_terminal("the proposal") is False
