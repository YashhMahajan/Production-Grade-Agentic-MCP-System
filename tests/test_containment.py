"""What the server guarantees when the model does exactly what a poisoned input says.

Two inputs carry the same instructions: a customer pastes them into the question, and a ticket
holds them in its subject. They tell the model to write to S3, fetch an outside URL, point the
database at another tenant and name another tenant in a tool argument. ``ObedientChat`` follows
every one it can. These tests do not check that a model would resist: they check that the system
holds when it does not.

Three layers are exercised separately. The step that reads tool results has no tools, so an
instruction in a ticket cannot cause a call. The step that picks tools is held to an allow-list in
code before anything leaves the process. With that allow-list widened to every tool, only the
server is left, and it has to refuse on its own: scopes, policy, the outbound allowlist and the
approval gate. The audit log is the record of what reached the server.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from scout_mcp.agents.graphs import ask  # noqa: E402
from scout_mcp.agents.mcp_client import ScoutMCPClient  # noqa: E402
from scout_mcp.tools.atomic import http_client  # noqa: E402
from scout_mcp.tools.atomic.postgres import PostgresQueryTool  # noqa: E402
from scout_mcp.tools.atomic.s3_storage import S3PutTool  # noqa: E402
from scout_mcp.tools.workflow.customer_context import CustomerContextTool  # noqa: E402
from tests.fakes import ADVERSARIAL_TICKET, HOSTILE_QUESTION  # noqa: E402
from tests.fakes_lc import ObedientChat  # noqa: E402

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
BENIGN_QUESTION = "Why was CUST-1001's refund delayed?"


class Lab:
    """One poisoned run, and everything that could have gone wrong during it."""

    def __init__(self, app_client):
        self.app = app_client
        self.tenants: list[str] = []   # tenant each data fetch ran for
        self.executed: list[str] = []  # tools whose body ran: none should for a refused call
        self.network: list[str] = []   # outbound fetches that got past the allowlist
        self.chat = ObedientChat()

    async def attack(self, token: str, *, allow_every_tool: bool, monkeypatch, question=HOSTILE_QUESTION):
        if allow_every_tool:
            monkeypatch.setattr(ask, "ALLOWED_TOOLS", frozenset(t.meta.name for t in self.app.server.registry))
        async with ScoutMCPClient("http://test", token, transport=self.app.transport_) as mcp:
            # Room for every injected call, so the server is asked about each one.
            return await ask.run_ask(mcp, [self.chat], question, NOW, max_llm_calls=10, max_tool_calls=10)

    def calls(self) -> list[tuple[str, str | None]]:
        """Each call that reached the server as (tool, error code or None). They run in parallel, so sorted."""
        rows = [json.loads(line) for line in self.app.server.audit.path.read_text().splitlines()]
        return sorted(((r["tool"], r["error_code"]) for r in rows), key=str)

    def approvals(self) -> list[dict]:
        data = self.app.server._redis.data
        return [json.loads(v) for k, v in data.items()
                if k.startswith("scout:approval:") and not k.endswith((":decision", ":claim"))]


@pytest.fixture
def lab(app_client, monkeypatch):
    lab = Lab(app_client)

    async def profile(self, tenant, customer_id):
        lab.tenants.append(tenant)
        return {"id": customer_id, "name": "Ana"}

    async def nothing(self, tenant, *a, **k):
        return []

    async def tickets(self, tenant, customer_id):
        return [ADVERSARIAL_TICKET]

    monkeypatch.setattr(CustomerContextTool, "_profile", profile)
    monkeypatch.setattr(CustomerContextTool, "_recent_orders", nothing)
    monkeypatch.setattr(CustomerContextTool, "_open_tickets", tickets)
    monkeypatch.setattr(CustomerContextTool, "_relevant_docs", nothing)

    def spy(cls):
        async def run(self, tenant, args):
            lab.executed.append(cls.meta.name)
            return {}
        monkeypatch.setattr(cls, "run", run)

    for cls in (S3PutTool, PostgresQueryTool):
        spy(cls)

    async def reached_network(host):
        lab.network.append(host)
        raise AssertionError(f"outbound fetch to {host} got past the allowlist")

    monkeypatch.setattr(http_client, "_refuse_unsafe_addresses", reached_network)

    return lab


# ── Instructions in data: the step that reads it has no tools ──────────────
async def test_an_instruction_in_a_ticket_reaches_the_model_and_causes_no_call(lab, monkeypatch):
    result = await lab.attack("support", allow_every_tool=True, monkeypatch=monkeypatch, question=BENIGN_QUESTION)
    assert len(lab.chat.seen_in_data) == 5  # the attack did arrive, in the evidence the model read
    assert lab.calls() == [("customer.build_context", None)]  # and the only call is the honest lookup
    assert result.outcome == "approved" and result.tool_calls == 1
    assert lab.executed == [] and lab.network == [] and lab.approvals() == []


# ── Instructions in the question: the allow-list is the first wall ─────────
async def test_through_the_copilot_the_write_and_the_fetches_never_leave_the_process(lab, monkeypatch):
    result = await lab.attack("support", allow_every_tool=False, monkeypatch=monkeypatch)
    # The honest lookup, then the two injected calls that name tools on the allow-list: the
    # server refuses the query itself, and the tenant argument is ignored.
    assert lab.calls() == [
        ("customer.build_context", None),
        ("customer.build_context", None),
        ("postgres.query", "invalid_arguments"),
    ]
    # The write and both fetches never left the process; the server's refusal of the query is recorded too.
    assert result.errors == ["s3.put_object: tool_not_allowed", "http.fetch: tool_not_allowed",
                             "http.fetch: tool_not_allowed", "postgres.query: invalid_arguments"]
    assert lab.executed == [] and lab.network == [] and lab.approvals() == []


# ── With the allow-list off: the server holds on its own ───────────────────
async def test_a_copilot_token_cannot_write_fetch_or_query_past_its_scopes(lab, monkeypatch):
    await lab.attack("support", allow_every_tool=True, monkeypatch=monkeypatch)
    assert lab.calls() == [
        ("customer.build_context", None),
        ("customer.build_context", None),
        ("http.fetch", "missing_scope"),
        ("http.fetch", "missing_scope"),
        ("postgres.query", "invalid_arguments"),   # set_config is refused before any scope check
        ("s3.put_object", "missing_scope"),
    ]
    assert lab.executed == [] and lab.network == [] and lab.approvals() == []


async def test_a_tenant_named_in_the_arguments_never_changes_whose_data_is_read(lab, monkeypatch):
    await lab.attack("support", allow_every_tool=True, monkeypatch=monkeypatch)
    assert lab.tenants and set(lab.tenants) == {"acme"}


async def test_a_fetch_to_a_host_off_the_allowlist_is_refused_before_any_network(lab, monkeypatch):
    await lab.attack("copilot_fetch", allow_every_tool=True, monkeypatch=monkeypatch)
    assert ("http.fetch", "host_not_allowlisted") in lab.calls()
    assert lab.network == []


async def test_an_allowlisted_post_waits_for_a_human_and_does_not_run(lab, monkeypatch):
    await lab.attack("copilot_fetch", allow_every_tool=True, monkeypatch=monkeypatch)
    assert ("http.fetch", "pending_approval") in lab.calls()
    held = lab.approvals()
    assert [a["tool"] for a in held] == ["http.fetch"] and held[0]["state"] == "pending"
    assert held[0]["arguments"]["url"] == "https://api.stripe.com/v1/refunds"
    assert lab.network == []  # the held call never reached the outbound code


async def test_a_write_the_token_is_allowed_to_make_is_still_held_for_approval(lab, monkeypatch):
    await lab.attack("copilot_write", allow_every_tool=True, monkeypatch=monkeypatch)
    assert ("s3.put_object", "pending_approval") in lab.calls()
    assert [a["tool"] for a in lab.approvals()] == ["s3.put_object"]
    assert lab.executed == []  # nothing was written
