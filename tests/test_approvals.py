"""Human review of destructive calls, over HTTP."""

from __future__ import annotations

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
