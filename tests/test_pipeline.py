"""The request path end to end, in-process: HTTP, auth, tenant, policy, tools, audit."""

from __future__ import annotations

import json

import httpx

from atlas_mcp.agents.mcp_client import AtlasMCPClient
from atlas_mcp.tools.atomic.postgres import PostgresQueryTool
from atlas_mcp.tools.atomic.s3_storage import S3PutTool
from tests.conftest import call, rpc

INIT = {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"}}


def fake_pg(calls):
    async def run(self, tenant, args):
        calls.append((tenant, args.sql))
        return {"columns": ["id"], "rows": [{"id": 1}], "row_count": 1, "truncated": False}
    return run


# ── Transport ─────────────────────────────────────────────────────────────
async def test_mcp_endpoint_answers_without_redirect_as_event_stream(app_client):
    resp, body = await rpc(app_client, "initialize", INIT)
    assert resp.status_code == 200  # was 307, then a crash on the missing task group
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert body["result"]["serverInfo"]["name"] == "atlas-mcp"


async def test_probes_and_discovery_need_no_token(app_client):
    assert (await app_client.get("/healthz")).status_code == 200
    assert (await app_client.get("/readyz")).json() == {"status": "ready"}
    doc = (await app_client.get("/.well-known/mcp-server")).json()
    assert doc["authorization_server"]["issuer"] == "https://auth.atlas.local"
    assert len(doc["tools_summary"]) == 9


async def test_mcp_requires_a_valid_token(app_client):
    resp = await app_client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


async def test_tenant_header_cannot_override_the_token(app_client):
    resp, _ = await rpc(app_client, "tools/list", token="support", tenant="globex")
    assert resp.status_code == 403


# ── Identity reaches the handlers ─────────────────────────────────────────
async def test_tools_list_shows_only_what_the_token_allows(app_client):
    _, body = await rpc(app_client, "tools/list", token="support")
    names = {t["name"] for t in body["result"]["tools"]}
    assert {"postgres.query", "elasticsearch.search", "vector.search"} <= names
    assert "s3.put_object" not in names
    _, body = await rpc(app_client, "tools/list", token="noscope")
    assert body["result"]["tools"] == []


async def test_call_runs_as_the_tokens_tenant(app_client, monkeypatch):
    calls = []
    monkeypatch.setattr(PostgresQueryTool, "run", fake_pg(calls))
    is_error, payload = await call(app_client, "postgres.query", {"sql": "SELECT id FROM customers"})
    assert not is_error and payload["row_count"] == 1
    assert calls == [("acme", "SELECT id FROM customers")]


async def test_a_hidden_tool_cannot_be_called_by_name(app_client):
    is_error, payload = await call(
        app_client, "postgres.query", {"sql": "SELECT 1"}, token="noscope"
    )
    assert is_error and payload["code"] == "missing_scope"


# ── Policy from config/policy.yaml ────────────────────────────────────────
async def test_policy_file_allows_a_normal_select(app_client, monkeypatch):
    monkeypatch.setattr(PostgresQueryTool, "run", fake_pg([]))
    is_error, _ = await call(app_client, "postgres.query", {"sql": "SELECT id FROM orders"})
    assert not is_error  # the mis-indented deny rule used to block every read


async def test_policy_file_denies_banned_keywords(app_client, monkeypatch):
    monkeypatch.setattr(PostgresQueryTool, "run", fake_pg([]))
    is_error, payload = await call(
        app_client, "postgres.query", {"sql": "SELECT id FROM orders WHERE note = 'drop'"}
    )
    assert is_error and payload["code"] == "denied_by_policy"


async def test_policy_file_denies_pii_columns_to_support_agents(app_client, monkeypatch):
    monkeypatch.setattr(PostgresQueryTool, "run", fake_pg([]))
    is_error, payload = await call(app_client, "postgres.query", {"sql": "SELECT ssn FROM customers"})
    assert is_error and payload["code"] == "denied_by_policy"


async def test_default_deny_applies_to_roles_without_a_rule(app_client, monkeypatch):
    monkeypatch.setattr(PostgresQueryTool, "run", fake_pg([]))
    # analysts may read analytics.* only
    is_error, payload = await call(
        app_client, "postgres.query", {"sql": "SELECT id FROM customers"}, token="analyst"
    )
    assert is_error and payload["code"] == "not_authorized"
    is_error, _ = await call(
        app_client, "postgres.query", {"sql": "SELECT id FROM analytics.orders"}, token="analyst"
    )
    assert not is_error


# ── Errors ────────────────────────────────────────────────────────────────
async def test_unknown_tool_returns_a_structured_error(app_client):
    is_error, payload = await call(app_client, "nope", {})
    assert is_error
    assert payload["code"] == "tool_not_found" and payload["retryable"] is False


async def test_invalid_arguments_return_a_hint(app_client):
    is_error, payload = await call(app_client, "postgres.query", {"sql": "DELETE FROM orders"})
    assert is_error and payload["code"] == "invalid_arguments"
    assert "SELECT" in payload["hint"]


async def test_unexpected_exceptions_do_not_leak(app_client, monkeypatch):
    async def boom(self, tenant, args):
        raise RuntimeError("password=hunter2")
    monkeypatch.setattr(PostgresQueryTool, "run", boom)
    is_error, payload = await call(app_client, "postgres.query", {"sql": "SELECT 1 FROM orders"})
    assert is_error and payload["code"] == "internal_error"
    assert "hunter2" not in json.dumps(payload)


# ── Agent client against the real server ──────────────────────────────────
async def test_agent_client_lists_tools_and_calls_one(app_client, monkeypatch):
    monkeypatch.setattr(PostgresQueryTool, "run", fake_pg([]))
    async with AtlasMCPClient(
        "http://test", "support", tenant="acme", transport=app_client.transport_
    ) as mcp:
        tools = await mcp.list_tools()
        assert "postgres.query" in {t["name"] for t in tools}
        ok = await mcp.call_tool("postgres.query", {"sql": "SELECT id FROM orders"})
        assert ok.ok and ok.value["row_count"] == 1
        bad = await mcp.call_tool("nope", {})
        assert not bad.ok and bad.error_code == "tool_not_found" and bad.retryable is False


# ── Approval gate ─────────────────────────────────────────────────────────
async def test_destructive_tools_wait_for_approval_then_run_once(app_client, monkeypatch):
    ran = []

    async def put(self, tenant, args):
        ran.append((tenant, args.key))
        return {"key": args.key, "bytes_written": len(args.content)}
    monkeypatch.setattr(S3PutTool, "run", put)
    args = {"key": "attachments/a.txt", "content": "hello"}

    is_error, payload = await call(app_client, "s3.put_object", args, token="writer")
    assert is_error and payload["code"] == "pending_approval" and not ran
    approval_id = payload["context"]["approval_id"]

    # Different arguments than the ones approved are refused.
    gate = app_client.server.approvals
    await gate.approve(approval_id, "user:boss")
    is_error, payload = await call(
        app_client, "s3.put_object", {**args, "content": "other", "approval_id": approval_id},
        token="writer",
    )
    assert is_error and payload["code"] == "approval_mismatch" and not ran

    is_error, _ = await call(
        app_client, "s3.put_object", {**args, "approval_id": approval_id}, token="writer"
    )
    assert not is_error and ran == [("acme", "attachments/a.txt")]

    # The approval is spent.
    is_error, payload = await call(
        app_client, "s3.put_object", {**args, "approval_id": approval_id}, token="writer"
    )
    assert is_error and payload["code"] == "approval_not_found"


async def test_unapproved_approval_id_is_refused(app_client):
    args = {"key": "attachments/a.txt", "content": "x"}
    _, payload = await call(app_client, "s3.put_object", args, token="writer")
    is_error, payload = await call(
        app_client, "s3.put_object",
        {**args, "approval_id": payload["context"]["approval_id"]}, token="writer",
    )
    assert is_error and payload["code"] == "approval_not_granted"


# ── Observability ─────────────────────────────────────────────────────────
async def test_audit_log_has_one_line_per_call_without_raw_arguments(app_client, monkeypatch):
    monkeypatch.setattr(PostgresQueryTool, "run", fake_pg([]))
    await call(app_client, "postgres.query", {"sql": "SELECT id FROM orders"})
    await call(app_client, "nope", {})
    path = app_client.server.audit.path
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(rec["tool"], rec["status"]) for rec in lines] == [
        ("postgres.query", "ok"), ("nope", "error")]
    assert lines[0]["tenant"] == "acme" and lines[0]["delegator"] == "user:ana"
    assert "SELECT" not in path.read_text()  # arguments are hashed, never written
    assert lines[1]["error_code"] == "tool_not_found"


async def test_metrics_endpoint_serves_the_servers_counters(app_client, monkeypatch):
    monkeypatch.setattr(PostgresQueryTool, "run", fake_pg([]))
    await call(app_client, "postgres.query", {"sql": "SELECT id FROM orders"})
    text = (await app_client.get("/metrics")).text
    assert 'atlas_tool_calls_total{status="ok",tool="postgres.query"} 1.0' in text


async def test_unknown_tool_names_do_not_create_metric_series(app_client):
    await call(app_client, "attacker-chosen-name-123", {})
    text = (await app_client.get("/metrics")).text
    assert "attacker-chosen-name-123" not in text
    assert 'tool="unknown"' in text


async def test_readyz_is_503_until_startup_finishes(monkeypatch, tmp_path):
    from atlas_mcp.config import ServerSettings
    from atlas_mcp.server import AtlasServer, build_http_app
    server = AtlasServer(ServerSettings(_env_file=None))
    app = build_http_app(server)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/readyz")).status_code == 503
        assert (await c.get("/healthz")).status_code == 200


async def test_each_call_gets_a_span_and_the_audit_line_carries_its_trace_id(
    app_client, monkeypatch
):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("atlas_mcp.server.get_tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(PostgresQueryTool, "run", fake_pg([]))

    await call(app_client, "postgres.query", {"sql": "SELECT id FROM orders"})
    await call(app_client, "nope", {})

    ok_span, bad_span = exporter.get_finished_spans()
    assert ok_span.name == "tool postgres.query" and ok_span.attributes["atlas.tenant"] == "acme"
    assert bad_span.name == "tool unknown"  # the caller-chosen name is not used
    assert bad_span.attributes["atlas.error_code"] == "tool_not_found"
    lines = [json.loads(x) for x in app_client.server.audit.path.read_text().splitlines()]
    assert lines[0]["trace_id"] == format(ok_span.context.trace_id, "032x")


async def test_results_with_timestamps_uuids_and_decimals_are_encoded(app_client, monkeypatch):
    # Postgres hands these back as Python objects; the MCP SDK cannot json.dumps them.
    import datetime
    import decimal
    import uuid

    async def run(self, tenant, args):
        return {"columns": ["t", "u", "d"], "row_count": 1, "truncated": False, "rows": [{
            "t": datetime.datetime(2026, 5, 12, 9, 14, tzinfo=datetime.timezone.utc),
            "u": uuid.UUID("12345678-1234-5678-1234-567812345678"),
            "d": decimal.Decimal("19.99")}]}
    monkeypatch.setattr(PostgresQueryTool, "run", run)
    is_error, payload = await call(app_client, "postgres.query", {"sql": "SELECT t FROM orders"})
    assert not is_error
    assert payload["rows"][0] == {"t": "2026-05-12 09:14:00+00:00",
                                  "u": "12345678-1234-5678-1234-567812345678", "d": "19.99"}
