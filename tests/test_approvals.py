"""Human review of destructive calls, over HTTP."""

from __future__ import annotations

import asyncio
import time

from scout_mcp.errors.framework import PolicyError
from scout_mcp.governance.approval import ApprovalGate
from scout_mcp.reliability.circuit_breaker import State
from scout_mcp.tools.atomic.http_client import HTTPFetchTool
from scout_mcp.tools.atomic.s3_storage import S3PutTool

from tests.conftest import HEADERS, call

ARGS = {"key": "attachments/a.txt", "content": "hello"}


async def pending(app_client) -> str:
    _, payload = await call(app_client, "s3.put_object", ARGS, token="writer")
    return payload["context"]["approval_id"]


def auth(token: str) -> dict:
    return {**HEADERS, "Authorization": f"Bearer {token}"}


async def test_reviewer_sees_the_exact_call_then_approves_and_it_runs(app_client, monkeypatch):
    ran = []

    async def put(self, tenant, args):
        ran.append(args.key)
        return {"key": args.key, "bytes_written": 5}
    monkeypatch.setattr(S3PutTool, "run", put)
    approval_id = await pending(app_client)

    detail = await app_client.get(f"/approvals/{approval_id}", headers=auth("reviewer"))
    assert detail.status_code == 200
    body = detail.json()
    assert body["tool"] == "s3.put_object" and body["arguments"] == {**ARGS, "content_type": "text/plain"}  # defaults included
    assert body["state"] == "pending" and body["caller"] == "agent:test"

    decided = await app_client.post(f"/approvals/{approval_id}/approve", headers=auth("reviewer"))
    assert decided.status_code == 200
    assert decided.json()["state"] == "approved" and decided.json()["approver"] == "user:reviewer"

    is_error, _ = await call(
        app_client, "s3.put_object", {**ARGS, "approval_id": approval_id}, token="writer")
    assert not is_error and ran == ["attachments/a.txt"]

    lines = app_client.server.audit.path.read_text()
    assert '"tool": "approval.approve"' in lines and "user:reviewer" in lines


async def test_denied_approval_blocks_the_call(app_client):
    approval_id = await pending(app_client)
    resp = await app_client.post(f"/approvals/{approval_id}/deny", headers=auth("reviewer"))
    assert resp.json()["state"] == "denied"
    is_error, payload = await call(
        app_client, "s3.put_object", {**ARGS, "approval_id": approval_id}, token="writer")
    assert is_error and payload["code"] == "approval_not_granted"


async def test_nobody_approves_their_own_request(app_client):
    approval_id = await pending(app_client)
    resp = await app_client.post(f"/approvals/{approval_id}/approve", headers=auth("self_reviewer"))
    assert resp.status_code == 403 and resp.json()["code"] == "self_approval"


async def test_review_needs_the_scope(app_client):
    approval_id = await pending(app_client)
    for path, method in ((f"/approvals/{approval_id}", "get"),
                         (f"/approvals/{approval_id}/approve", "post")):
        resp = await getattr(app_client, method)(path, headers=auth("no_review_scope"))
        assert resp.status_code == 403 and resp.json()["code"] == "missing_scope"


async def test_another_tenants_reviewer_is_refused(app_client):
    approval_id = await pending(app_client)
    resp = await app_client.post(
        f"/approvals/{approval_id}/approve", headers=auth("reviewer_globex"))
    assert resp.status_code == 403 and resp.json()["code"] == "approval_tenant_mismatch"


async def test_a_decision_cannot_be_changed(app_client):
    approval_id = await pending(app_client)
    await app_client.post(f"/approvals/{approval_id}/approve", headers=auth("reviewer"))
    resp = await app_client.post(f"/approvals/{approval_id}/deny", headers=auth("reviewer"))
    assert resp.status_code == 409 and resp.json()["code"] == "approval_already_decided"


async def test_unknown_approval_and_unknown_action(app_client):
    resp = await app_client.post("/approvals/nope/approve", headers=auth("reviewer"))
    assert resp.status_code == 404
    approval_id = await pending(app_client)
    resp = await app_client.post(f"/approvals/{approval_id}/delete", headers=auth("reviewer"))
    assert resp.status_code == 404


async def test_approval_routes_require_a_token(app_client):
    assert (await app_client.get("/approvals/x")).status_code == 401


async def test_one_approval_runs_the_call_once_when_spent_five_times_at_once(
    app_client, monkeypatch
):
    ran = []

    async def put(self, tenant, args):
        await asyncio.sleep(0.05)  # keep the first call in flight while the others check
        ran.append(args.key)
        return {"key": args.key, "bytes_written": 5}
    monkeypatch.setattr(S3PutTool, "run", put)
    approval_id = await pending(app_client)
    await app_client.server.approvals.approve(approval_id, "user:reviewer")

    results = await asyncio.gather(*[
        call(app_client, "s3.put_object", {**ARGS, "approval_id": approval_id}, token="writer")
        for _ in range(5)
    ])
    assert len(ran) == 1
    errors = [payload["code"] for is_error, payload in results if is_error]
    assert errors == ["approval_already_used"] * 4


async def test_two_reviewers_deciding_at_once_get_one_winner(app_client):
    approval_id = await pending(app_client)
    gate = app_client.server.approvals
    outcomes = await asyncio.gather(
        gate.approve(approval_id, "user:a"), gate.deny(approval_id, "user:b"),
        return_exceptions=True,
    )
    losers = [o for o in outcomes if isinstance(o, PolicyError)]
    assert len(losers) == 1 and losers[0].code == "approval_already_decided"
    final = await gate.check(approval_id, "acme")
    assert final.state in ("approved", "denied")


async def test_a_decision_does_not_extend_the_requests_lifetime(app_client):
    approval_id = await pending(app_client)
    fake = app_client.server._redis
    fake.ttls[ApprovalGate._key(approval_id)] = 100  # forty-odd minutes already used
    await app_client.server.approvals.approve(approval_id, "user:reviewer")
    assert fake.ttls[ApprovalGate._decision_key(approval_id)] == 100


async def test_an_approval_survives_a_call_the_circuit_breaker_refused(app_client, monkeypatch):
    ran = []

    async def put(self, tenant, args):
        ran.append(args.key)
        return {"key": args.key, "bytes_written": 5}
    monkeypatch.setattr(S3PutTool, "run", put)
    approval_id = await pending(app_client)
    gate = app_client.server.approvals
    await gate.approve(approval_id, "user:reviewer")
    breaker = app_client.server.breakers.for_tool("s3.put_object")
    breaker._state, breaker._opened_at = State.OPEN, time.monotonic()

    is_error, payload = await call(
        app_client, "s3.put_object", {**ARGS, "approval_id": approval_id}, token="writer")
    assert is_error and payload["code"] == "circuit_open" and not ran
    assert (await gate.check(approval_id, "acme")).state == "approved"  # not spent


async def test_http_fetch_get_runs_but_every_other_method_waits_for_a_human(
    app_client, monkeypatch
):
    seen = []

    async def run(self, tenant, args):
        seen.append(args.method)
        return {"status": 200, "headers": {}, "body": "", "truncated": False}
    monkeypatch.setattr(HTTPFetchTool, "run", run)
    url = "https://api.company-status-page.internal/x"

    is_error, _ = await call(app_client, "http.fetch", {"url": url}, token="fetcher")
    assert not is_error
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        is_error, payload = await call(
            app_client, "http.fetch", {"url": url, "method": method, "body": {}}, token="fetcher")
        assert is_error and payload["code"] == "pending_approval", method
    assert seen == ["GET"]
