"""`scout-mcp ingest`: chunk, embed and write one document to all three stores, per tenant.

The stores are fakes that behave like the real ones where it matters here: Qdrant is an
httpx mock that evaluates filters, Postgres refuses to overwrite another tenant's row.
"""

from __future__ import annotations

import json

import httpx
import pytest

from scout_mcp.cli import main
from scout_mcp.ingest import (
    Document, ElasticsearchStore, IngestError, Ingestor, PostgresStore, QdrantStore,
    chunk_text, documents_from_files, embedding_mismatch, point_id,
)

DIM = 4


# ── Chunking ───────────────────────────────────────────────────────────────
def test_short_text_is_one_chunk():
    assert chunk_text("Refunds take 5 days.") == ["Refunds take 5 days."]


def test_paragraphs_are_packed_up_to_the_limit_and_never_split_when_they_fit():
    paras = [f"Paragraph {i}. " + "word " * 30 for i in range(6)]
    chunks = chunk_text("\n\n".join(paras), max_chars=400)
    assert all(len(c) <= 400 for c in chunks) and len(chunks) > 1
    assert "".join(chunks).replace("\n\n", "").replace(" ", "") == "".join(paras).replace(" ", "")


def test_one_paragraph_longer_than_the_limit_is_cut_at_sentences_then_hard():
    sentence = "This sentence is exactly fifty characters long ok. "
    chunks = chunk_text(sentence * 20, max_chars=200)
    assert all(len(c) <= 200 for c in chunks)
    assert all(c.endswith(".") for c in chunks)  # cut at sentence ends
    assert all(len(c) <= 100 for c in chunk_text("x" * 450, max_chars=100))  # no sentence to cut at


def test_no_text_is_no_chunks():
    assert chunk_text("  \n\n ") == []


# ── Files to documents ─────────────────────────────────────────────────────
def test_files_become_documents_with_tenant_prefixed_ids(tmp_path):
    (tmp_path / "Refund Policy.md").write_text("# Refund policy\n\nRefunds take 5 days.")
    (tmp_path / "shipping.txt").write_text("Standard shipping is 3-5 days.")
    docs = documents_from_files("acme", [tmp_path / "Refund Policy.md", tmp_path / "shipping.txt"])
    assert [(d.id, d.title, d.tenant) for d in docs] == [
        ("doc_acme_refund_policy", "Refund policy", "acme"),
        ("doc_acme_shipping", "shipping", "acme"),
    ]


@pytest.mark.parametrize("tenant", ["Acme", "a b", "../x", "", "-x"])
def test_a_tenant_name_that_is_not_a_tenant_is_refused(tmp_path, tenant):
    (tmp_path / "a.md").write_text("hello")
    with pytest.raises(IngestError, match="tenant"):
        documents_from_files(tenant, [tmp_path / "a.md"])


def test_only_text_files_are_read(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    with pytest.raises(IngestError, match="a.pdf"):
        documents_from_files("acme", [tmp_path / "a.pdf"])


def test_an_empty_file_is_refused(tmp_path):
    (tmp_path / "a.md").write_text("  \n")
    with pytest.raises(IngestError, match="empty"):
        documents_from_files("acme", [tmp_path / "a.md"])


# ── Fakes ──────────────────────────────────────────────────────────────────
class FakeES:
    class _Indices:
        def __init__(self, es):
            self.es = es

        async def exists(self, index):
            return index in self.es.indices_created

        async def create(self, index, mappings):
            self.es.indices_created[index] = mappings

    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}
        self.indices_created: dict[str, dict] = {}
        self.indices = self._Indices(self)

    def options(self, **_):
        return self

    async def get(self, index, id, source_includes=None):
        doc = self.docs.get((index, id))
        return {"found": True, "_source": doc} if doc else {"found": False}

    async def index(self, index, id, document):
        self.docs[(index, id)] = document

    async def close(self):
        pass


class FakeQdrant:
    def __init__(self):
        self.collections: dict[str, int] = {}
        self.points: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle), base_url="http://qdrant")

    @staticmethod
    def _matches(payload, flt):
        for cond in flt.get("must", []):
            value = payload.get(cond["key"])
            if "match" in cond and value != cond["match"]["value"]:
                return False
            if "range" in cond and not (value is not None and value >= cond["range"]["gte"]):
                return False
        return True

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.requests.append((request.method, path, body))
        name = path.split("/")[2]
        if path == f"/collections/{name}" and request.method == "GET":
            if name not in self.collections:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"size": self.collections[name]}}}}})
        if path == f"/collections/{name}" and request.method == "PUT":
            self.collections[name] = body["vectors"]["size"]
            return httpx.Response(200, json={"result": True})
        if path.endswith("/points") and request.method == "PUT":
            for p in body["points"]:
                self.points[p["id"]] = p["payload"]
            return httpx.Response(200, json={"result": {}})
        if path.endswith("/points/count"):
            n = sum(self._matches(p, body["filter"]) for p in self.points.values())
            return httpx.Response(200, json={"result": {"count": n}})
        if path.endswith("/points/delete"):
            self.points = {i: p for i, p in self.points.items() if not self._matches(p, body["filter"])}
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(500, json={"error": f"unexpected {request.method} {path}"})


class FakeConn:
    def __init__(self, owners):
        self.owners, self.log, self.tenant = owners, [], None

    async def execute(self, sql, *args):
        self.log.append(("execute", sql, args))
        if sql.startswith("SET app.tenant_id"):
            self.tenant = sql.split("'")[1]

    async def fetchrow(self, sql, *args):
        self.log.append(("fetchrow", sql, args))
        doc_id, tenant = args[0], args[1]
        if self.owners.get(doc_id, tenant) != tenant:
            return None  # ON CONFLICT ... WHERE tenant matches: another tenant's row is left alone
        self.owners[doc_id] = tenant
        return {"id": doc_id}

    async def close(self):
        self.log.append(("close",))


class Lab:
    def __init__(self):
        self.es, self.qdrant, self.owners = FakeES(), FakeQdrant(), {}
        self.embedded: list[list[str]] = []
        self.conns: list[FakeConn] = []

    async def embed(self, texts):
        self.embedded.append(list(texts))
        return [[float(len(t) % 7), 1.0, 0.0, 0.5] for t in texts]

    async def connect(self):
        conn = FakeConn(self.owners)
        self.conns.append(conn)
        return conn

    def ingestor(self, *, postgres=True, **kw) -> Ingestor:
        return Ingestor(
            embed=self.embed, model="test-model",
            es=ElasticsearchStore(self.es, "documents"),
            qdrant=QdrantStore(self.qdrant.client(), "support_docs"),
            postgres=PostgresStore(self.connect) if postgres else None, **kw)


@pytest.fixture
def lab():
    return Lab()


def doc(tenant="acme", id="doc_acme_refunds", body="Refunds take 5 days.", title="Refunds"):
    return Document(id=id, tenant=tenant, title=title, body=body)


# ── Writing ────────────────────────────────────────────────────────────────
async def test_a_document_reaches_all_three_stores_tagged_with_its_tenant(lab):
    report = await lab.ingestor().run([doc()])
    assert (report.documents, report.chunks, report.skipped) == (1, 1, 0)

    es_doc = lab.es.docs[("documents", "doc_acme_refunds")]
    assert es_doc["_tenant"] == "acme" and es_doc["title"] == "Refunds"

    (payload,) = lab.qdrant.points.values()
    assert payload == {"tenant": "acme", "doc_id": "doc_acme_refunds", "chunk": 0, "title": "Refunds",
                       "text": "Refunds take 5 days."}

    (conn,) = lab.conns
    kinds = [e[0] for e in conn.log]
    assert kinds == ["execute", "fetchrow", "close"]  # tenant is set before the row is written
    assert conn.log[0][1] == "SET app.tenant_id = 'acme'" and conn.log[1][2][:2] == ("doc_acme_refunds", "acme")


async def test_the_text_that_is_embedded_carries_the_title(lab):
    await lab.ingestor().run([doc()])
    assert lab.embedded == [["Refunds. Refunds take 5 days."]]


async def test_an_unchanged_document_is_not_embedded_again(lab):
    await lab.ingestor().run([doc()])
    report = await lab.ingestor().run([doc()])
    assert (report.documents, report.skipped) == (0, 1) and len(lab.embedded) == 1


async def test_force_embeds_an_unchanged_document_again(lab):
    await lab.ingestor().run([doc()])
    report = await lab.ingestor(force=True).run([doc()])
    assert report.documents == 1 and len(lab.embedded) == 2


async def test_a_document_that_shrinks_loses_its_stale_chunks(lab):
    long_body = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(3))
    big = doc(body=long_body)
    first = await lab.ingestor(max_chars=400).run([big])
    assert first.chunks > 1 and len(lab.qdrant.points) == first.chunks

    report = await lab.ingestor(max_chars=400).run([doc(body="Now it is short.")])
    assert len(lab.qdrant.points) == 1 and report.stale_deleted == first.chunks - 1
    assert next(iter(lab.qdrant.points.values()))["text"] == "Now it is short."


async def test_re_ingesting_one_tenant_never_touches_another_tenants_chunks(lab):
    # A chunk of another tenant that happens to carry the same doc_id (an old collision).
    lab.qdrant.points["legacy"] = {"tenant": "globex", "doc_id": "doc_refunds", "chunk": 7, "text": "globex"}
    big = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(3))
    await lab.ingestor(max_chars=400).run([doc(id="doc_refunds", body=big)])
    await lab.ingestor(max_chars=400).run([doc(id="doc_refunds", body="Now short.")])
    assert lab.qdrant.points["legacy"]["text"] == "globex"
    deletes = [b for m, p, b in lab.qdrant.requests if p.endswith("/points/delete")]
    assert deletes and all({"key": "tenant", "match": {"value": "acme"}} in b["filter"]["must"] for b in deletes)


async def test_a_document_id_owned_by_another_tenant_in_elasticsearch_is_refused(lab):
    lab.es.docs[("documents", "doc_x")] = {"_tenant": "globex", "_sha": "old"}
    with pytest.raises(IngestError, match="another tenant"):
        await lab.ingestor().run([doc(tenant="acme", id="doc_x")])
    assert lab.es.docs[("documents", "doc_x")]["_tenant"] == "globex" and lab.qdrant.points == {}


async def test_two_tenants_with_the_same_document_id_get_different_point_ids():
    assert point_id("acme", "doc_a", 0) != point_id("globex", "doc_a", 0)
    assert point_id("acme", "doc_a", 0) == point_id("acme", "doc_a", 0)


async def test_a_row_that_belongs_to_another_tenant_is_an_error_and_is_not_marked_done(lab):
    lab.owners["doc_x"] = "globex"
    with pytest.raises(IngestError, match="another tenant"):
        await lab.ingestor().run([doc(tenant="acme", id="doc_x")])
    assert ("documents", "doc_x") not in lab.es.docs  # a retry will do the work again


async def test_a_collection_built_with_a_different_dimension_is_refused_before_any_write(lab):
    lab.qdrant.collections["support_docs"] = 768
    with pytest.raises(IngestError, match="768"):
        await lab.ingestor().run([doc()])
    assert lab.qdrant.points == {} and lab.es.docs == {}


async def test_the_index_and_collection_are_created_when_missing(lab):
    await lab.ingestor().run([doc()])
    assert "documents" in lab.es.indices_created and lab.qdrant.collections == {"support_docs": DIM}


async def test_postgres_can_be_left_out(lab):
    await lab.ingestor(postgres=False).run([doc()])
    assert lab.conns == [] and lab.es.docs


# ── The embedding model ────────────────────────────────────────────────────
async def test_the_embedding_model_is_recorded_when_chunks_are_embedded(lab):
    await lab.ingestor().run([doc()])
    assert lab.es.docs[("scout_meta", "embedding-support_docs")] == {"model": "test-model", "dim": DIM}


async def test_a_server_embedding_with_another_model_is_reported(lab):
    await lab.ingestor().run([doc()])
    assert await embedding_mismatch(lab.es, "support_docs", "test-model") is None
    warning = await embedding_mismatch(lab.es, "support_docs", "other-model")
    assert "test-model" in warning and "other-model" in warning


async def test_nothing_recorded_is_not_a_warning(lab):
    assert await embedding_mismatch(lab.es, "support_docs", "any-model") is None


# ── The command ────────────────────────────────────────────────────────────
def test_ingest_command_reads_the_files_and_reports(tmp_path, monkeypatch, capsys):
    (tmp_path / "refunds.md").write_text("# Refunds\n\nFive days.")
    seen = {}

    async def fake(documents, **kw):
        seen["docs"], seen["kw"] = documents, kw
        from scout_mcp.ingest import IngestReport
        return IngestReport(documents=len(documents), chunks=2, skipped=0, stale_deleted=0)
    monkeypatch.setattr("scout_mcp.cli.ingest_documents", fake)
    code = main(["ingest", "--tenant", "acme", str(tmp_path / "refunds.md")])
    assert code == 0 and [d.tenant for d in seen["docs"]] == ["acme"]
    assert "1 document written, 2 chunks embedded" in capsys.readouterr().out


def test_ingest_command_refuses_a_bad_tenant_without_touching_a_store(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.md").write_text("hello")
    monkeypatch.setattr("scout_mcp.cli.ingest_documents", lambda *a, **k: pytest.fail("reached a store"))
    assert main(["ingest", "--tenant", "Bad Tenant", str(tmp_path / "a.md")]) == 1
    assert "tenant" in capsys.readouterr().err


# ── Chunked documents in hybrid search ─────────────────────────────────────
async def test_hybrid_search_ranks_a_document_by_its_best_chunk(monkeypatch):
    from scout_mcp.auth.policy import PolicyEngine
    from scout_mcp.tools.composed import hybrid_search
    from scout_mcp.tools.composed.hybrid_search import HybridSearchInput, HybridSearchTool

    class Emb:
        async def embed(self, texts):
            return [[1.0, 0.0]]
    monkeypatch.setattr(hybrid_search, "get_embedder", lambda: Emb())
    tool = HybridSearchTool(PolicyEngine(rules=[], default_deny=False))

    async def es_run(tenant, args):
        return {"total": 0, "took_ms": 1, "hits": []}

    async def vec_run(tenant, args):
        # doc_a matches first, doc_b second, then another chunk of doc_a.
        return {"matches": [
            {"id": 1, "doc_id": "doc_a", "score": .9, "payload": {}},
            {"id": 2, "doc_id": "doc_b", "score": .8, "payload": {}},
            {"id": 3, "doc_id": "doc_a", "score": .1, "payload": {}}]}
    monkeypatch.setattr(tool.es_tool, "run", es_run)
    monkeypatch.setattr(tool.vector_tool, "run", vec_run)
    out = await tool.run("acme", HybridSearchInput(query="refund", es_index="documents", vector_collection="support_docs"))
    ranks = {r["id"]: r["dense_rank"] for r in out["results"]}
    assert ranks == {"doc_a": 1, "doc_b": 2}


# ── The server's startup check ─────────────────────────────────────────────
async def test_startup_warns_when_the_collection_was_embedded_with_another_model(lab, monkeypatch, caplog):
    from scout_mcp import server as srv
    from scout_mcp.config import ServerSettings
    await lab.ingestor().run([doc()])  # records "test-model"
    monkeypatch.setattr(srv, "AsyncElasticsearch", lambda *a, **k: lab.es)
    server = srv.ScoutServer(ServerSettings(_env_file=None, embedding_model="other-model"))
    with caplog.at_level("WARNING"):
        await server._check_embedding_model()
    assert "test-model" in caplog.text and "other-model" in caplog.text


async def test_startup_does_not_fail_when_elasticsearch_cannot_be_reached(monkeypatch, caplog):
    from scout_mcp import server as srv
    from scout_mcp.config import ServerSettings

    class Down:
        def options(self, **_):
            return self

        async def get(self, **_):
            raise ConnectionError("elasticsearch is down")

        async def close(self):
            pass
    monkeypatch.setattr(srv, "AsyncElasticsearch", lambda *a, **k: Down())
    server = srv.ScoutServer(ServerSettings(_env_file=None))
    with caplog.at_level("WARNING"):
        await server._check_embedding_model()  # must not raise
    assert "could not check" in caplog.text


async def test_the_check_can_be_switched_off():
    from scout_mcp.config import ServerSettings
    assert ServerSettings(_env_file=None).check_embedding_model is True
    assert ServerSettings(_env_file=None, check_embedding_model=False).check_embedding_model is False
