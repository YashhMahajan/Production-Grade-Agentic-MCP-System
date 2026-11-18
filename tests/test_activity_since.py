"""``customer.activity_since`` through the server: what it asks the stores, and how it fails."""

from __future__ import annotations

from datetime import datetime, timezone

from scout_mcp.errors.framework import UpstreamError
from scout_mcp.tools.atomic.elasticsearch import ElasticsearchSearchTool
from scout_mcp.tools.atomic.postgres import PostgresQueryTool
from scout_mcp.tools.workflow.customer_activity import CustomerActivityTool
from tests.conftest import call, rpc

ARGS = {"customer_id": "CUST-1001", "since": "2026-11-09T12:00:00Z"}
ORDER = {"id": "o_2", "status": "shipped", "total_cents": 1250, "currency": "USD", "created_at": "2026-11-12T00:00:00Z"}
HIT = {"id": "t_7", "score": 1.0, "source": {"subject": "Late", "status": "open", "priority": "high",
                                              "created_at": "2026-11-13T09:00:00Z"}}


def stores(monkeypatch, *, orders=None, hits=None, pg_error=None, es_error=None):
    """Replace the two stores. Returns the list of (store, tenant, input) the tool asked."""
    asked = []

    async def pg(self, tenant, args):
        asked.append(("postgres", tenant, args))
        if pg_error:
            raise pg_error
        return {"columns": [], "rows": orders or [], "row_count": len(orders or []), "truncated": False}

    async def es(self, tenant, args):
        asked.append(("elasticsearch", tenant, args))
        if es_error:
            raise es_error
        return {"total": len(hits or []), "took_ms": 1, "hits": hits or []}

    monkeypatch.setattr(PostgresQueryTool, "run", pg)
    monkeypatch.setattr(ElasticsearchSearchTool, "run", es)
    return asked


async def test_new_orders_and_tickets_come_back_in_one_structured_result(app_client, monkeypatch):
    stores(monkeypatch, orders=[ORDER], hits=[HIT])
    is_error, out = await call(app_client, "customer.activity_since", ARGS)
    assert not is_error
    assert out["since"] == "2026-11-09T12:00:00+00:00"
    assert out["orders"] == [ORDER]
    assert out["tickets"] == [{"id": "t_7", "subject": "Late", "status": "open", "priority": "high",
                               "created_at": "2026-11-13T09:00:00Z"}]
    assert "partial_errors" not in out


async def test_the_orders_query_binds_the_customer_and_a_zoned_time(app_client, monkeypatch):
    asked = stores(monkeypatch, orders=[ORDER])
    await call(app_client, "customer.activity_since", {**ARGS, "since": "2026-11-09T12:00:00", "limit": 5})
    (_, tenant, query) = next(a for a in asked if a[0] == "postgres")
    assert tenant == "acme" and query.max_rows == 5
    assert "customer_id = $1 AND created_at > $2" in query.sql
    assert query.params == ["CUST-1001", datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)]  # no zone means UTC


async def test_the_tickets_query_is_one_customer_after_the_time(app_client, monkeypatch):
    asked = stores(monkeypatch, hits=[HIT])
    await call(app_client, "customer.activity_since", ARGS)
    (_, _, query) = next(a for a in asked if a[0] == "elasticsearch")
    assert query.index == "tickets"
    assert query.query == {"bool": {"must": [{"term": {"customer_id": "CUST-1001"}},
                                             {"range": {"created_at": {"gt": "2026-11-09T12:00:00+00:00"}}}]}}


async def test_a_store_that_is_down_is_named_and_the_other_half_is_still_served(app_client, monkeypatch):
    stores(monkeypatch, orders=[ORDER], es_error=UpstreamError("elasticsearch_error", retryable=True, hint="down"))
    is_error, out = await call(app_client, "customer.activity_since", ARGS)
    assert not is_error and out["orders"] == [ORDER] and "tickets" not in out
    assert out["partial_errors"] == {"tickets": "elasticsearch_error"}


async def test_an_unexpected_failure_is_reported_without_its_message(app_client, monkeypatch):
    stores(monkeypatch, hits=[HIT], pg_error=RuntimeError("password=hunter2"))
    _, out = await call(app_client, "customer.activity_since", ARGS)
    assert out["partial_errors"] == {"orders": "internal_error: RuntimeError"}
    assert "hunter2" not in str(out)


async def test_the_tenant_is_the_callers_and_never_an_argument(app_client, monkeypatch):
    asked = stores(monkeypatch)
    await call(app_client, "customer.activity_since", {**ARGS, "tenant": "globex"})
    assert {tenant for _, tenant, _ in asked} <= {"acme"}  # whether the extra field is ignored or refused


async def test_arguments_that_do_not_fit_are_refused_before_any_store_is_asked(app_client, monkeypatch):
    asked = stores(monkeypatch)
    for bad in ({**ARGS, "since": "last week"}, {**ARGS, "limit": 51}, {**ARGS, "limit": 0},
                {**ARGS, "customer_id": "CUST-1; DROP TABLE orders"}, {"customer_id": "CUST-1001"}):
        is_error, out = await call(app_client, "customer.activity_since", bad)
        assert is_error and out["code"] == "invalid_arguments", bad
    assert asked == []


async def test_a_caller_without_both_read_scopes_is_refused(app_client, monkeypatch):
    asked = stores(monkeypatch)
    for token in ("analyst", "noscope"):  # postgres only, and nothing
        is_error, out = await call(app_client, "customer.activity_since", ARGS, token=token)
        assert is_error and out["code"] == "missing_scope", token
    assert asked == []


async def test_the_tool_is_listed_with_an_output_schema_and_no_write_flag(app_client):
    _, body = await rpc(app_client, "tools/list", token="support")
    tool = next(t for t in body["result"]["tools"] if t["name"] == "customer.activity_since")
    assert set(tool["outputSchema"]["properties"]) >= {"customer_id", "since", "orders", "tickets", "partial_errors"}
    assert CustomerActivityTool.meta.read_only and not CustomerActivityTool.meta.destructive
