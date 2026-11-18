"""``ticket.escalate`` through the server: nothing runs until a human approves, and only that call runs, once."""

from __future__ import annotations

import pytest
from elasticsearch import TransportError

from scout_mcp.tools.atomic.elasticsearch import ElasticsearchSearchTool
from scout_mcp.tools.atomic.ticket_escalate import TicketEscalateTool
from tests.conftest import HEADERS, call, rpc

ARGS = {"ticket_id": "t_5002", "reason": "Past its SLA and the customer has written twice."}


class FakeEs:
    def __init__(self, updated=1, error=None):
        self.updated, self.error, self.calls = updated, error, []

    async def update_by_query(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"updated": self.updated}


@pytest.fixture
def es(monkeypatch):
    fake = FakeEs()
    monkeypatch.setattr(ElasticsearchSearchTool, "client", lambda self: fake)
    return fake


def reviewer() -> dict:
    return {**HEADERS, "Authorization": "Bearer reviewer"}


async def propose(app_client, args=ARGS, token="copilot_escalate") -> str:
    is_error, payload = await call(app_client, "ticket.escalate", args, token=token)
    assert is_error and payload["code"] == "pending_approval", payload
    return payload["context"]["approval_id"]


async def approve(app_client, approval_id) -> None:
    resp = await app_client.post(f"/approvals/{approval_id}/approve", headers=reviewer())
    assert resp.status_code == 200 and resp.json()["state"] == "approved"


# ── nothing runs until a human approves ────────────────────────────────────
async def test_a_call_waits_for_a_human_and_touches_nothing(app_client, es):
    approval_id = await propose(app_client)
    assert es.calls == []
    shown = (await app_client.get(f"/approvals/{approval_id}", headers=reviewer())).json()
    assert shown["tool"] == "ticket.escalate" and shown["state"] == "pending"
    assert shown["arguments"] == ARGS and shown["caller"] == "agent:test"


async def test_the_approved_call_raises_the_ticket_in_the_callers_tenant_only(app_client, es):
    approval_id = await propose(app_client)
    await approve(app_client, approval_id)
    is_error, out = await call(app_client, "ticket.escalate", {**ARGS, "approval_id": approval_id},
                               token="copilot_escalate")
    assert not is_error
    assert out["ticket_id"] == "t_5002" and out["priority"] == "high" and out["escalated_at"]
    (sent,) = es.calls
    assert sent["index"] == "tickets" and sent["refresh"] is True
    assert sent["query"] == {"bool": {"filter": [{"ids": {"values": ["t_5002"]}}, {"term": {"_tenant": "acme"}}]}}
    assert sent["script"]["params"] == {"at": out["escalated_at"], "reason": ARGS["reason"]}
    assert "ctx._source.priority = 'high'" in sent["script"]["source"]


async def test_an_approval_runs_once(app_client, es):
    approval_id = await propose(app_client)
    await approve(app_client, approval_id)
    first, _ = await call(app_client, "ticket.escalate", {**ARGS, "approval_id": approval_id}, token="copilot_escalate")
    again, out = await call(app_client, "ticket.escalate", {**ARGS, "approval_id": approval_id},
                            token="copilot_escalate")
    assert not first and again and out["code"] == "approval_already_used" and len(es.calls) == 1


async def test_the_approved_call_is_the_one_that_runs(app_client, es):
    approval_id = await propose(app_client)
    await approve(app_client, approval_id)
    changed = {"ticket_id": "t_5001", "reason": ARGS["reason"], "approval_id": approval_id}
    is_error, out = await call(app_client, "ticket.escalate", changed, token="copilot_escalate")
    assert is_error and out["code"] == "approval_mismatch" and es.calls == []


async def test_a_denied_request_never_runs(app_client, es):
    approval_id = await propose(app_client)
    await app_client.post(f"/approvals/{approval_id}/deny", headers=reviewer())
    is_error, out = await call(app_client, "ticket.escalate", {**ARGS, "approval_id": approval_id},
                               token="copilot_escalate")
    assert is_error and out["code"] == "approval_not_granted" and es.calls == []


async def test_the_copilot_that_asked_cannot_approve_its_own_request(app_client, es):
    approval_id = await propose(app_client)
    resp = await app_client.post(f"/approvals/{approval_id}/approve",
                                 headers={**HEADERS, "Authorization": "Bearer copilot_escalate"})
    assert resp.status_code == 403 and es.calls == []


# ── what it does when it runs ──────────────────────────────────────────────
async def test_a_ticket_that_is_not_the_tenants_is_not_found(app_client, monkeypatch):
    fake = FakeEs(updated=0)
    monkeypatch.setattr(ElasticsearchSearchTool, "client", lambda self: fake)
    approval_id = await propose(app_client)
    await approve(app_client, approval_id)
    is_error, out = await call(app_client, "ticket.escalate", {**ARGS, "approval_id": approval_id},
                               token="copilot_escalate")
    assert is_error and out["code"] == "invalid_arguments" and "no ticket t_5002" in out["hint"]


async def test_a_store_that_is_down_is_reported_as_an_upstream_error(app_client, monkeypatch):
    monkeypatch.setattr(ElasticsearchSearchTool, "client", lambda self: FakeEs(error=TransportError("down")))
    approval_id = await propose(app_client)
    await approve(app_client, approval_id)
    is_error, out = await call(app_client, "ticket.escalate", {**ARGS, "approval_id": approval_id},
                               token="copilot_escalate")
    assert is_error and out["code"] == "elasticsearch_error"


# ── who may ask, and what may be asked ─────────────────────────────────────
async def test_a_caller_without_the_scope_cannot_even_ask(app_client, es):
    is_error, out = await call(app_client, "ticket.escalate", ARGS, token="support")
    assert is_error and out["code"] in ("missing_scope", "not_authorized")
    assert not [k for k in app_client.server._redis.data if k.startswith("scout:approval:")]


@pytest.mark.parametrize("bad", [
    {"ticket_id": "t_5002", "reason": ""}, {"ticket_id": "t_5002", "reason": "   "},
    {"ticket_id": "t_5002", "reason": "x" * 501}, {"ticket_id": "../t_5002", "reason": "why"},
    {"ticket_id": "t 5002", "reason": "why"}, {"ticket_id": "", "reason": "why"}, {"reason": "why"},
])
async def test_arguments_that_do_not_fit_are_refused_before_any_approval_is_made(app_client, es, bad):
    is_error, out = await call(app_client, "ticket.escalate", bad, token="copilot_escalate")
    assert is_error and out["code"] == "invalid_arguments"
    assert not [k for k in app_client.server._redis.data if k.startswith("scout:approval:")]


async def test_the_tool_is_listed_only_for_a_caller_who_may_use_it(app_client):
    async def listed(token):
        _, body = await rpc(app_client, "tools/list", token=token)
        return {t["name"]: t for t in body["result"]["tools"]}

    assert "ticket.escalate" not in await listed("support")
    tool = (await listed("copilot_escalate"))["ticket.escalate"]
    assert set(tool["outputSchema"]["properties"]) == {"ticket_id", "priority", "escalated_at"}
    assert TicketEscalateTool.meta.destructive and not TicketEscalateTool.meta.read_only
