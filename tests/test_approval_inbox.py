"""The approval inbox, and the optional signed webhook that says a request is waiting.

A reviewer used to need the approval id from the agent or a log. Now they can list what is
waiting for them, and an operator can be told when something new arrives.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import ValidationError

from scout_mcp.config import ServerSettings
from scout_mcp.governance.approval import APPROVAL_TTL_SECONDS
from scout_mcp.governance.webhook import ApprovalNotifier
from tests.conftest import HEADERS, call

ARGS = {"key": "attachments/a.txt", "content": "hello"}


def auth(token: str) -> dict:
    return {**HEADERS, "Authorization": f"Bearer {token}"}


async def ask(app_client, token="writer", **over) -> str:
    _, payload = await call(app_client, "s3.put_object", {**ARGS, **over}, token=token)
    return payload["context"]["approval_id"]


async def inbox(app_client, token="reviewer", query="?state=pending"):
    return await app_client.get(f"/approvals{query}", headers=auth(token))


# ── The inbox ──────────────────────────────────────────────────────────────
async def test_a_reviewer_sees_what_is_waiting_soonest_to_expire_first(app_client):
    ticks = iter(range(1000, 2000))
    app_client.server.approvals._clock = lambda: next(ticks)
    first = await ask(app_client, key="attachments/one.txt")
    second = await ask(app_client, key="attachments/two.txt")
    resp = await inbox(app_client)
    assert resp.status_code == 200
    listed = resp.json()["approvals"]
    assert [a["id"] for a in listed] == [first, second]
    assert {"id", "tool", "caller", "delegator", "created_at", "expires_in_seconds"} <= set(listed[0])
    assert listed[0]["tool"] == "s3.put_object" and listed[0]["caller"] == "agent:test"


async def test_the_list_never_carries_the_arguments(app_client):
    await ask(app_client, content="SECRET-CONTENT")
    body = (await inbox(app_client)).text
    assert "SECRET-CONTENT" not in body and "arguments" not in body


async def test_a_decided_request_leaves_the_inbox(app_client):
    waiting, decided = await ask(app_client, key="attachments/a.txt"), await ask(app_client, key="attachments/b.txt")
    await app_client.post(f"/approvals/{decided}/deny", headers=auth("reviewer"))
    assert [a["id"] for a in (await inbox(app_client)).json()["approvals"]] == [waiting]


async def test_an_expired_request_is_dropped_and_forgotten(app_client):
    gate = app_client.server.approvals
    await ask(app_client)
    later = gate._clock() + APPROVAL_TTL_SECONDS + 1
    gate._clock = lambda: later
    assert (await inbox(app_client)).json()["approvals"] == []
    assert not any(k.startswith("scout:approvals:") and v for k, v in app_client.server._redis.zsets.items())


async def test_another_tenants_reviewer_sees_nothing_of_this_tenants_requests(app_client):
    await ask(app_client)
    assert (await inbox(app_client, token="reviewer_globex")).json()["approvals"] == []


async def test_an_id_planted_in_another_tenants_index_is_not_shown(app_client):
    approval_id = await ask(app_client)  # an acme request
    app_client.server._redis.zsets.setdefault("scout:approvals:globex", {})[approval_id] = 10**12
    assert (await inbox(app_client, token="reviewer_globex")).json()["approvals"] == []


async def test_a_decision_takes_the_request_off_the_index_not_just_out_of_the_list(app_client):
    approval_id = await ask(app_client)
    index = app_client.server._redis.zsets["scout:approvals:acme"]
    assert approval_id in index
    await app_client.post(f"/approvals/{approval_id}/approve", headers=auth("reviewer"))
    assert approval_id not in index


async def test_a_reviewer_is_not_shown_what_they_could_not_decide(app_client):
    await ask(app_client)  # agent:test acting for user:ana
    assert (await inbox(app_client, token="ana_reviewer")).json()["approvals"] == []
    assert len((await inbox(app_client, token="reviewer")).json()["approvals"]) == 1


@pytest.mark.parametrize("token, code", [("no_review_scope", "missing_scope"), ("scope_only", "missing_role")])
async def test_listing_needs_both_the_scope_and_the_role(app_client, token, code):
    await ask(app_client)
    resp = await inbox(app_client, token=token)
    assert resp.status_code == 403 and resp.json()["code"] == code


async def test_only_pending_can_be_listed(app_client):
    assert (await inbox(app_client, query="")).status_code == 200  # pending is the default
    resp = await inbox(app_client, query="?state=approved")
    assert resp.status_code == 400 and resp.json()["error"] == "unsupported_state"


async def test_the_inbox_does_not_swallow_the_detail_routes(app_client):
    approval_id = await ask(app_client)
    resp = await app_client.get(f"/approvals/{approval_id}", headers=auth("reviewer"))
    assert resp.status_code == 200 and resp.json()["id"] == approval_id and "arguments" in resp.json()


# ── The webhook ────────────────────────────────────────────────────────────
SECRET = "whsec-test"
URL = "https://hooks.example.com/scout"


class Receiver:
    def __init__(self, fail=False):
        self.requests: list[httpx.Request] = []
        self.fail = fail

    def transport(self):
        def handle(request):
            self.requests.append(request)
            if self.fail:
                raise httpx.ConnectError("receiver is down")
            return httpx.Response(204)
        return httpx.MockTransport(handle)


def wire(app_client, receiver) -> ApprovalNotifier:
    notifier = ApprovalNotifier(URL, SECRET, transport=receiver.transport())
    app_client.server.notifier = notifier
    return notifier


async def test_a_new_request_is_announced_with_ids_only_and_a_signature(app_client):
    receiver = Receiver()
    notifier = wire(app_client, receiver)
    approval_id = await ask(app_client, content="SECRET-CONTENT")
    await notifier.drain()

    (request,) = receiver.requests
    assert str(request.url) == URL and request.method == "POST"
    body = json.loads(request.content)
    assert set(body) == {"event", "approval_id", "tenant", "tool", "created_at"}
    assert body["event"] == "approval.requested" and body["approval_id"] == approval_id
    assert body["tenant"] == "acme" and body["tool"] == "s3.put_object"
    assert b"SECRET-CONTENT" not in request.content and b"attachments" not in request.content

    timestamp = request.headers["x-scout-timestamp"]
    expected = hmac.new(SECRET.encode(), f"{timestamp}.".encode() + request.content, hashlib.sha256).hexdigest()
    assert request.headers["x-scout-signature"] == f"sha256={expected}"


async def test_a_webhook_that_is_down_does_not_fail_or_hold_up_the_call(app_client):
    receiver = Receiver(fail=True)
    notifier = wire(app_client, receiver)
    approval_id = await ask(app_client)  # the agent still gets its approval id
    await notifier.drain()
    assert len(receiver.requests) == 1
    assert [a["id"] for a in (await inbox(app_client)).json()["approvals"]] == [approval_id]


async def test_the_call_returns_while_the_webhook_is_still_in_flight(app_client):
    release = asyncio.Event()

    async def blocked(request):
        await release.wait()
        return httpx.Response(204)
    notifier = ApprovalNotifier(URL, SECRET, transport=httpx.MockTransport(blocked))
    app_client.server.notifier = notifier
    await asyncio.wait_for(ask(app_client), timeout=1)  # would hang if the send were awaited inline
    assert len(notifier._tasks) == 1
    release.set()
    await notifier.drain()
    assert not notifier._tasks


async def test_nothing_is_sent_when_no_webhook_is_configured(app_client):
    assert app_client.server.notifier is None
    await ask(app_client)  # must simply work


async def test_a_slow_receiver_is_cut_off(app_client):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(204)
    notifier = ApprovalNotifier(URL, SECRET, transport=httpx.MockTransport(slow), timeout_s=0.05)
    app_client.server.notifier = notifier
    started = asyncio.get_event_loop().time()
    await ask(app_client)
    await notifier.drain()
    assert asyncio.get_event_loop().time() - started < 2


# ── Settings ───────────────────────────────────────────────────────────────
def test_a_webhook_url_without_a_secret_is_a_startup_error():
    with pytest.raises(ValidationError, match="secret"):
        ServerSettings(_env_file=None, approval_webhook_url=URL)


def test_a_plain_http_webhook_is_a_production_problem():
    s = ServerSettings(_env_file=None, approval_webhook_url="http://hooks.example.com/x", approval_webhook_secret="s")
    assert any("approval_webhook_url" in p for p in s.production_problems())
    ok = ServerSettings(_env_file=None, approval_webhook_url=URL, approval_webhook_secret="s")
    assert not any("approval_webhook_url" in p for p in ok.production_problems())
