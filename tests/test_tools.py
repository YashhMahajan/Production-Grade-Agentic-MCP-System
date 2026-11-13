"""Tool behaviour that does not need a live backend."""

from __future__ import annotations

import pytest

from atlas_mcp.agents.planner import PlannerAgent
from atlas_mcp.auth.policy import PolicyEngine
from atlas_mcp.tools.atomic import embeddings
from atlas_mcp.tools.atomic.postgres import PostgresQueryInput, PostgresQueryTool
from atlas_mcp.tools.atomic.vector_search import VectorSearchTool
from atlas_mcp.tools.composed import hybrid_search, semantic_search
from atlas_mcp.tools.composed.semantic_search import SemanticSearchInput, SemanticSearchTool

POLICY = PolicyEngine(rules=[], default_deny=False)


# ── Postgres ──────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, rows):
        self.rows, self.asked = rows, None

    async def fetch(self, n):
        self.asked = n
        return self.rows[:n]


class FakeConn:
    def __init__(self, rows):
        self.log, self.cursor_obj, self.rows = [], None, rows

    def transaction(self, readonly=False):
        conn = self

        class Tx:
            async def __aenter__(self):
                conn.log.append(("begin", readonly))

            async def __aexit__(self, *exc):
                conn.log.append(("end",))
        return Tx()

    async def execute(self, sql, *params):
        self.log.append(("execute", sql, params))

    async def cursor(self, sql, *params):
        self.log.append(("cursor", sql, params))
        self.cursor_obj = FakeCursor(self.rows)
        return self.cursor_obj


class FakePool:
    def __init__(self, rows):
        self.conn = FakeConn(rows)

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                pass
        return Ctx()


async def run_pg(rows, **kw):
    tool = PostgresQueryTool(POLICY)
    pool = FakePool(rows)

    async def ensure():
        return pool
    tool._ensure_pool = ensure
    args = PostgresQueryInput(sql=kw.pop("sql", "SELECT id FROM orders"), **kw)
    return await tool.run("acme", args), pool.conn


async def test_postgres_sets_tenant_inside_a_read_only_transaction():
    result, conn = await run_pg([{"id": 1}])
    assert conn.log[0] == ("begin", True)
    # SET LOCAL cannot take a bind parameter; set_config can, and is transaction-local.
    assert conn.log[1] == ("execute", "SELECT set_config('app.tenant_id', $1, true)", ("acme",))
    assert conn.log[2][0] == "cursor"
    assert result["row_count"] == 1


async def test_postgres_does_not_append_limit_to_the_query():
    _, conn = await run_pg([{"id": 1}], sql="SELECT id FROM orders ORDER BY id LIMIT 5")
    sql = conn.log[2][1]
    assert sql == "SELECT id FROM orders ORDER BY id LIMIT 5"  # a second LIMIT is a syntax error


async def test_postgres_reports_truncation_exactly():
    rows = [{"id": i} for i in range(4)]
    result, conn = await run_pg(rows, max_rows=3)
    assert conn.cursor_obj.asked == 4  # one extra row to detect the cut
    assert result["row_count"] == 3 and result["truncated"] is True
    result, _ = await run_pg(rows[:3], max_rows=3)
    assert result["truncated"] is False  # exactly max_rows rows is not truncated


def test_postgres_policy_resource_and_columns():
    tool = PostgresQueryTool(POLICY)
    args = PostgresQueryInput(sql="SELECT ssn, name FROM analytics.orders")
    assert tool.policy_resource(args) == "analytics/orders"
    assert {"ssn", "name"} <= set(tool.policy_context(args)["columns"])


# ── Embeddings ────────────────────────────────────────────────────────────
def test_building_search_tools_does_not_load_the_model(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("embedding model loaded at construction")
    monkeypatch.setattr(embeddings, "EmbeddingClient", forbidden)
    embeddings.get_embedder.cache_clear()
    semantic_search.SemanticSearchTool(POLICY)
    hybrid_search.HybridSearchTool(POLICY)


def test_one_embedder_is_shared(monkeypatch):
    built = []

    class Fake:
        def __init__(self):
            built.append(1)
    monkeypatch.setattr(embeddings, "EmbeddingClient", Fake)
    embeddings.get_embedder.cache_clear()
    assert embeddings.get_embedder() is embeddings.get_embedder()
    assert len(built) == 1
    embeddings.get_embedder.cache_clear()


def test_default_model_is_bge_base():
    from atlas_mcp.config import ServerSettings
    assert ServerSettings(_env_file=None).embedding_model == "BAAI/bge-base-en-v1.5"


# ── Search joins on the document id, not the Qdrant point id ──────────────
async def test_semantic_search_hydrates_by_payload_doc_id(monkeypatch):
    class FakeEmbedder:
        async def embed(self, texts):
            return [[0.1, 0.2]]
    monkeypatch.setattr(semantic_search, "get_embedder", lambda: FakeEmbedder())
    tool = SemanticSearchTool(POLICY)
    seen = {}

    async def vector_run(tenant, args):
        # point id is a UUID; the document id is in the payload
        return {"matches": [{"id": "6f1c-uuid", "doc_id": "doc_refund_policy", "score": 0.9,
                             "payload": {"doc_id": "doc_refund_policy", "tenant": "acme"}}]}

    async def pg_run(tenant, args):
        seen["params"] = args.params
        return {"rows": [{"id": "doc_refund_policy", "title": "Refund policy",
                          "body": "5 business days", "url": "u"}]}
    tool.vector_tool.run, tool.postgres_tool.run = vector_run, pg_run
    out = await tool.run("acme", SemanticSearchInput(query="refund", collection="support_docs"))
    assert seen["params"] == [["doc_refund_policy"]]
    assert out["results"][0]["id"] == "doc_refund_policy"
    assert out["results"][0]["title"] == "Refund policy"


async def test_vector_tool_exposes_doc_id(monkeypatch):
    import httpx
    payload = {"result": [{"id": "uuid-1", "score": 0.5, "payload": {"doc_id": "doc_a"}},
                          {"id": 7, "score": 0.4, "payload": {}}]}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return payload

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json, headers=None): return Resp()
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    from atlas_mcp.tools.atomic.vector_search import VectorSearchInput
    out = await VectorSearchTool(POLICY).run("acme", VectorSearchInput(collection="c", vector=[1.0]))
    assert [m["doc_id"] for m in out["matches"]] == ["doc_a", 7]


# ── Planner ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("Why was the refund on order o_9002 for CUST-1001 delayed?", "CUST-1001"),
    ("cust_77 asked about shipping", "CUST_77"),
    ("customers are angry about accounting", None),
    ("no id here", None),
])
def test_planner_extracts_the_full_customer_id(text, expected):
    assert PlannerAgent._extract_customer_id(text) == expected
