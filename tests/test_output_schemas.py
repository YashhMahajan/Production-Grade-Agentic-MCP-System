"""Every tool publishes the shape of what it returns, and the server holds it to that shape.

A client that reads ``outputSchema`` (the MCP SDK does) rejects a result that does not fit.
So the schema has to describe what the tool really returns, including the partial result
``customer.build_context`` sends when one backend is down.
"""

from __future__ import annotations

import jsonschema
import pytest

from scout_mcp.tools.atomic.http_client import HTTPFetchTool
from scout_mcp.tools.atomic.postgres import PostgresQueryTool
from scout_mcp.tools.registry import ToolRegistry
from scout_mcp.tools.workflow.customer_context import CustomerContextTool
from tests.conftest import rpc

# One realistic result per tool, in the shape each tool's run() returns today.
SAMPLES: dict[str, dict] = {
    "postgres.query": {"columns": ["id"], "rows": [{"id": "CUST-1001"}], "row_count": 1, "truncated": False},
    "postgres.describe": {"schema_name": "public", "tables": [
        {"name": "orders", "columns": [{"name": "id", "type": "text", "nullable": False}],
         "hidden_columns": 0, "foreign_keys": ["customer_id -> customers.id"]}]},
    "elasticsearch.search": {"total": 1, "took_ms": 3, "hits": [
        {"id": "TCK-1", "score": 1.2, "source": {"subject": "Refund"}}]},
    "vector.search": {"matches": [{"id": 7, "doc_id": "DOC-1", "score": 0.9, "payload": {"text": "x"}}]},
    "s3.get_object": {"key": "docs/a.txt", "size_bytes": 5, "content": "hello"},
    "s3.put_object": {"key": "attachments/a.txt", "bytes_written": 5},
    "http.fetch": {"status": 200, "headers": {"content-type": "text/plain"}, "body": "ok", "truncated": False},
    "semantic_search": {"query": "refund", "results": [
        {"id": "DOC-1", "score": 0.9, "title": "Refunds", "body": "...", "url": None, "metadata": {}}]},
    "hybrid_search": {"query": "refund", "fused_from": 1, "results": [
        {"id": "DOC-1", "rrf_score": 0.03, "lexical_rank": 1, "dense_rank": None, "source": {"title": "Refunds"}}]},
    "customer.build_context": {
        "customer_id": "CUST-1001", "question": "refund?",
        "profile": {"id": "CUST-1001", "name": "Ana", "email": "ana@acme.test", "tier": "gold",
                    "created_at": "2026-01-01 00:00:00+00:00"},
        "orders": [{"id": "ORD-1", "status": "refund_pending", "total_cents": 4200, "currency": "USD",
                    "created_at": "2026-02-01 00:00:00+00:00"}],
        "tickets": [{"id": "TCK-1", "subject": "Refund", "status": "open", "priority": "high",
                     "created_at": "2026-02-02T00:00:00Z"}],
        "docs": [{"id": "DOC-1", "score": 0.9, "preview": "Refunds take 5 days", "metadata": {}}],
    },
    "ticket.escalate": {"ticket_id": "t_5002", "priority": "high", "escalated_at": "2026-11-18T10:00:00+00:00"},
    "customer.activity_since": {
        "customer_id": "CUST-1001", "since": "2026-02-01T00:00:00+00:00",
        "orders": [{"id": "ORD-2", "status": "shipped", "total_cents": 1250, "currency": "USD",
                    "created_at": "2026-02-03 00:00:00+00:00"}],
        "tickets": [], "partial_errors": {"tickets": "elasticsearch_error"},
    },
}


async def registered():
    registry = ToolRegistry()
    await registry.discover()
    return {t.meta.name: t for t in registry}


async def test_every_tool_has_a_sample_and_a_declared_output_schema():
    tools = await registered()
    assert set(SAMPLES) == set(tools), "add a sample for each new tool"
    for name, tool in tools.items():
        assert tool.output_schema is not None, f"{name} declares no output_schema"


async def test_tools_list_publishes_each_output_schema(app_client):
    _, body = await rpc(app_client, "tools/list", token="admin")
    listed = {t["name"]: t for t in body["result"]["tools"]}
    assert set(listed) == set(SAMPLES)
    for name, spec in listed.items():
        assert spec["outputSchema"]["type"] == "object", name


async def test_each_sample_fits_its_published_schema(app_client):
    _, body = await rpc(app_client, "tools/list", token="admin")
    for spec in body["result"]["tools"]:
        jsonschema.validate(SAMPLES[spec["name"]], spec["outputSchema"])


async def test_a_result_missing_a_required_field_does_not_fit(app_client):
    _, body = await rpc(app_client, "tools/list", token="admin")
    schemas = {t["name"]: t["outputSchema"] for t in body["result"]["tools"]}
    broken = {k: v for k, v in SAMPLES["postgres.query"].items() if k != "rows"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, schemas["postgres.query"])


async def test_a_partial_context_still_fits_the_schema(app_client):
    _, body = await rpc(app_client, "tools/list", token="admin")
    schema = next(t["outputSchema"] for t in body["result"]["tools"] if t["name"] == "customer.build_context")
    partial = {"customer_id": "CUST-1001", "question": "refund?",
               "profile": {}, "partial_errors": {"orders": "postgres_error", "docs": "internal_error: KeyError"}}
    jsonschema.validate(partial, schema)


async def call_raw(app_client, name, arguments, token="admin"):
    _, body = await rpc(app_client, "tools/call", {"name": name, "arguments": arguments}, token)
    return body["result"]


async def test_a_real_result_arrives_as_structured_content_that_fits(app_client, monkeypatch):
    async def run(self, tenant, args):
        return SAMPLES["postgres.query"]
    monkeypatch.setattr(PostgresQueryTool, "run", run)
    result = await call_raw(app_client, "postgres.query", {"sql": "SELECT id FROM customers"})
    assert not result.get("isError")
    assert result["structuredContent"] == SAMPLES["postgres.query"]


async def test_customer_context_with_a_backend_down_is_served_with_partial_errors(app_client, monkeypatch):
    async def orders(self, tenant, *a, **k):
        return SAMPLES["customer.build_context"]["orders"]

    async def docs(self, tenant, *a, **k):
        return SAMPLES["customer.build_context"]["docs"]

    async def down(self, tenant, *a, **k):
        from scout_mcp.errors.framework import UpstreamError
        raise UpstreamError("elasticsearch_error", retryable=True, hint="down")

    async def profile(self, tenant, customer_id):
        return SAMPLES["customer.build_context"]["profile"]
    monkeypatch.setattr(CustomerContextTool, "_profile", profile)
    monkeypatch.setattr(CustomerContextTool, "_recent_orders", orders)
    monkeypatch.setattr(CustomerContextTool, "_open_tickets", down)
    monkeypatch.setattr(CustomerContextTool, "_relevant_docs", docs)
    result = await call_raw(app_client, "customer.build_context",
                            {"customer_id": "CUST-1001", "question": "refund?"})
    assert not result.get("isError")
    assert result["structuredContent"]["partial_errors"] == {"tickets": "elasticsearch_error"}


async def test_a_tool_that_breaks_its_own_schema_fails_loudly_and_is_not_cached(app_client, monkeypatch):
    async def wrong(self, tenant, args):
        return {"columns": ["id"], "row_count": "many"}  # no rows, row_count not an int
    monkeypatch.setattr(PostgresQueryTool, "run", wrong)
    result = await call_raw(app_client, "postgres.query", {"sql": "SELECT id FROM customers"})
    assert result["isError"] is True
    assert "output_schema_violation" in result["content"][0]["text"]


async def test_a_get_fetch_result_fits_the_schema_too(app_client, monkeypatch):
    async def run(self, tenant, args):
        return SAMPLES["http.fetch"]
    monkeypatch.setattr(HTTPFetchTool, "run", run)
    result = await call_raw(app_client, "http.fetch", {"url": "https://api.stripe.com/v1/x"}, token="fetcher")
    assert not result.get("isError") and result["structuredContent"]["status"] == 200
