"""``scout-mcp ingest``: put documents into Elasticsearch, Qdrant and Postgres for one tenant.

Each document is cut into chunks, the chunks are embedded with the model the server uses,
and the three stores are written in an order that makes a crash safe:

1. Qdrant: the new chunks, then the chunks of this document that no longer exist.
2. Postgres: the document row (a separate, narrow role; the server's role cannot write).
3. Elasticsearch: the document, with a hash of its content. This goes last, so a document
   is only ever marked done once everything else has been written. Run the same files
   again and unchanged documents are skipped without embedding anything.

Every write names its tenant. A document id that belongs to another tenant is an error,
never an overwrite.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from scout_mcp.tenant_sql import set_tenant_sql

DEFAULT_COLLECTION = "support_docs"
DEFAULT_INDEX = "documents"
META_INDEX = "scout_meta"

# Same rule the rest of the system uses for a tenant name: lower case, safe in a key and a filter.
TENANT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# Exact-match fields must be `keyword`: with the default mapping "CUST-1001" is split into
# tokens so a term query never matches it, and a term on _tenant "acme" also matches "acme-corp".
DOCUMENT_MAPPINGS = {"properties": {
    "title": {"type": "text"}, "body": {"type": "text"},
    "_tenant": {"type": "keyword"}, "_sha": {"type": "keyword"},
}}

_UPSERT_DOCUMENT = (
    "INSERT INTO documents (id, tenant_id, title, body, url) VALUES ($1, $2, $3, $4, $5) "
    "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, body = EXCLUDED.body, url = EXCLUDED.url "
    "WHERE documents.tenant_id = EXCLUDED.tenant_id RETURNING id"
)


class IngestError(Exception):
    pass


@dataclass(frozen=True)
class Document:
    id: str
    tenant: str
    title: str
    body: str
    url: str | None = None

    @property
    def sha(self) -> str:
        blob = "\0".join((self.title, self.body, self.url or "")).encode()
        return hashlib.sha256(blob).hexdigest()


@dataclass
class IngestReport:
    documents: int = 0      # documents written
    chunks: int = 0         # chunks embedded
    skipped: int = 0        # documents whose content had not changed
    stale_deleted: int = 0  # chunks removed because their document got shorter


# ── Reading files ──────────────────────────────────────────────────────────
def check_tenant(tenant: str) -> None:
    if not TENANT.match(tenant):
        raise IngestError(f"{tenant!r} is not a tenant name: lower case letters, digits, - and _")


def documents_from_files(tenant: str, paths: Sequence[Path]) -> list[Document]:
    """One document per ``.md`` or ``.txt`` file. A leading ``# heading`` becomes the title."""
    check_tenant(tenant)
    documents = []
    for path in map(Path, paths):
        if path.suffix.lower() not in (".md", ".txt"):
            raise IngestError(f"{path.name}: only .md and .txt files are read")
        text = path.read_text(encoding="utf-8").strip()
        title = path.stem
        if text.startswith("# "):
            heading, _, text = text.partition("\n")
            title, text = heading[2:].strip(), text.strip()
        if not text:
            raise IngestError(f"{path.name} is empty")
        slug = re.sub(r"[^a-z0-9]+", "_", path.stem.lower()).strip("_")
        documents.append(Document(id=f"doc_{tenant}_{slug}", tenant=tenant, title=title, body=text))
    return documents


# ── Chunking ───────────────────────────────────────────────────────────────
def chunk_text(text: str, max_chars: int = 1000) -> list[str]:
    """Pack paragraphs into chunks of at most ``max_chars``.

    A paragraph that is too long is cut at sentence ends, and a sentence that is still too
    long is cut at ``max_chars``. A chunk never starts or ends mid-word unless one word is longer
    than the limit.
    """
    pieces: list[tuple[str, str]] = []  # (separator before it, text)
    for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()):
        sentences = [paragraph] if len(paragraph) <= max_chars else re.split(r"(?<=[.!?])\s+", paragraph)
        for i, sentence in enumerate(sentences):
            for j in range(0, len(sentence), max_chars):
                first = i == 0 and j == 0
                pieces.append(("\n\n" if first else " " if j == 0 else "", sentence[j:j + max_chars]))
    chunks: list[str] = []
    current = ""
    for sep, piece in pieces:
        if current and len(current) + len(sep) + len(piece) <= max_chars:
            current += sep + piece
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def point_id(tenant: str, doc_id: str, n: int) -> str:
    """Qdrant ids must be integers or UUIDs. The tenant is in it so two tenants never collide."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"scout/{tenant}/{doc_id}#{n}"))


# ── Stores ─────────────────────────────────────────────────────────────────
def _body(response) -> dict:
    return getattr(response, "body", response)


class ElasticsearchStore:
    def __init__(self, es, index: str = DEFAULT_INDEX):
        self.es, self.index = es, index

    async def ensure_index(self) -> None:
        if not await self.es.indices.exists(index=self.index):
            await self.es.indices.create(index=self.index, mappings=DOCUMENT_MAPPINGS)

    async def stored_sha(self, doc: Document) -> str | None:
        """The hash this document was last written with, or None. Another tenant's id is an error."""
        found = _body(await self.es.options(ignore_status=404).get(
            index=self.index, id=doc.id, source_includes=["_sha", "_tenant"]))
        if not found.get("found"):
            return None
        source = found["_source"]
        if source.get("_tenant") != doc.tenant:
            raise IngestError(f"{doc.id} already belongs to another tenant; pick a different id")
        return source.get("_sha")

    async def upsert(self, doc: Document) -> None:
        document = {"title": doc.title, "body": doc.body, "_tenant": doc.tenant, "_sha": doc.sha}
        if doc.url:
            document["url"] = doc.url
        await self.es.index(index=self.index, id=doc.id, document=document)

    async def record_embedding(self, collection: str, model: str, dim: int) -> None:
        await self.es.index(index=META_INDEX, id=f"embedding-{collection}",
                            document={"model": model, "dim": dim})


class QdrantStore:
    def __init__(self, http: httpx.AsyncClient, collection: str = DEFAULT_COLLECTION):
        self.http, self.collection = http, collection
        self._ready = False

    async def ensure_collection(self, dim: int) -> None:
        if self._ready:
            return
        url = f"/collections/{self.collection}"
        found = await self.http.get(url)
        if found.status_code == 404:
            (await self.http.put(url, json={"vectors": {"size": dim, "distance": "Cosine"}})).raise_for_status()
        else:
            found.raise_for_status()
            size = found.json()["result"]["config"]["params"]["vectors"]["size"]
            if size != dim:
                raise IngestError(
                    f"collection {self.collection} holds {size}-dimension vectors but this model makes "
                    f"{dim}: ingest into a new collection, or re-ingest everything with the same model")
        self._ready = True

    async def upsert(self, doc: Document, chunks: list[str], vectors: list[list[float]]) -> None:
        points = [
            {"id": point_id(doc.tenant, doc.id, n), "vector": vector,
             "payload": {"tenant": doc.tenant, "doc_id": doc.id, "chunk": n, "title": doc.title, "text": text}}
            for n, (text, vector) in enumerate(zip(chunks, vectors, strict=True))
        ]
        response = await self.http.put(f"/collections/{self.collection}/points",
                                       params={"wait": "true"}, json={"points": points})
        response.raise_for_status()

    async def delete_stale(self, doc: Document, keep: int) -> int:
        """Remove this tenant's chunks of this document numbered ``keep`` or higher. Returns how many."""
        flt = {"must": [{"key": "tenant", "match": {"value": doc.tenant}},
                        {"key": "doc_id", "match": {"value": doc.id}},
                        {"key": "chunk", "range": {"gte": keep}}]}
        base = f"/collections/{self.collection}/points"
        counted = await self.http.post(f"{base}/count", json={"filter": flt, "exact": True})
        counted.raise_for_status()
        stale = counted.json()["result"]["count"]
        if stale:
            (await self.http.post(f"{base}/delete", params={"wait": "true"}, json={"filter": flt})).raise_for_status()
        return stale


class PostgresStore:
    """Writes the document row as a role that may write ``documents`` and nothing else.

    The tenant is set on the connection first, so row-level security applies to the write.
    """

    def __init__(self, connect: Callable[[], Awaitable]):
        self._connect = connect
        self._conn = None
        self._tenant: str | None = None

    async def upsert(self, doc: Document) -> None:
        if self._conn is None:
            self._conn = await self._connect()
        if self._tenant != doc.tenant:
            await self._conn.execute(set_tenant_sql(doc.tenant, local=False))
            self._tenant = doc.tenant
        row = await self._conn.fetchrow(_UPSERT_DOCUMENT, doc.id, doc.tenant, doc.title, doc.body, doc.url)
        if row is None:
            raise IngestError(f"{doc.id} already belongs to another tenant; pick a different id")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


# ── The run ────────────────────────────────────────────────────────────────
class Ingestor:
    """Writes documents to the stores it is given. ``qdrant`` and ``postgres`` may be left out."""

    def __init__(
        self, *, embed: Callable[[list[str]], Awaitable[list[list[float]]]], model: str,
        es: ElasticsearchStore, qdrant: QdrantStore | None = None,
        postgres: PostgresStore | None = None, max_chars: int = 1000, force: bool = False,
    ):
        self.embed, self.model, self.es = embed, model, es
        self.qdrant, self.postgres = qdrant, postgres
        self.max_chars, self.force = max_chars, force

    async def run(self, documents: Sequence[Document]) -> IngestReport:
        report = IngestReport()
        dim: int | None = None
        try:
            await self.es.ensure_index()
            for doc in documents:
                if not self.force and await self.es.stored_sha(doc) == doc.sha:
                    report.skipped += 1
                    continue
                if self.qdrant is not None:
                    chunks = chunk_text(doc.body, self.max_chars)
                    vectors = await self.embed([f"{doc.title}. {c}" for c in chunks])
                    dim = len(vectors[0])
                    await self.qdrant.ensure_collection(dim)
                    await self.qdrant.upsert(doc, chunks, vectors)
                    report.stale_deleted += await self.qdrant.delete_stale(doc, keep=len(chunks))
                    report.chunks += len(chunks)
                if self.postgres is not None:
                    await self.postgres.upsert(doc)
                await self.es.upsert(doc)  # last: the document counts as done only now
                report.documents += 1
            if dim is not None and self.qdrant is not None:
                await self.es.record_embedding(self.qdrant.collection, self.model, dim)
        finally:
            if self.postgres is not None:
                await self.postgres.close()
        return report


# ── What the server checks at startup ──────────────────────────────────────
async def embedding_mismatch(es, collection: str, model: str) -> str | None:
    """A warning if ``collection`` was embedded with a different model than ``model``, else None.

    Vectors from two models cannot be compared, and when both have the same dimension nothing
    errors: search just returns the wrong passages. Nothing recorded is not a warning.
    """
    found = _body(await es.options(ignore_status=404).get(index=META_INDEX, id=f"embedding-{collection}"))
    if not found.get("found"):
        return None
    recorded = found["_source"].get("model")
    if recorded == model:
        return None
    return (f"collection {collection} was embedded with {recorded} but this server embeds queries "
            f"with {model}; search results will be wrong until the documents are re-ingested")


# ── The real clients, for the command ──────────────────────────────────────
async def ingest_documents(
    documents: Sequence[Document], *, collection: str = DEFAULT_COLLECTION,
    index: str = DEFAULT_INDEX, force: bool = False, max_chars: int = 1000,
    only_elasticsearch: bool = False,
) -> IngestReport:
    """Ingest into the stores named by the usual SCOUT_ settings.

    Postgres is written only if SCOUT_INGEST_POSTGRES_DSN is set, as a role that can write
    ``documents``. The server's own DSN is read-only and is never used here.
    """
    import asyncpg
    from elasticsearch import AsyncElasticsearch

    from scout_mcp.config import get_settings
    from scout_mcp.tools.atomic.embeddings import get_embedder

    settings = get_settings()
    password = settings.elasticsearch_password
    es = AsyncElasticsearch(settings.elasticsearch_url,
                            basic_auth=(settings.elasticsearch_user, password) if password else None)
    headers = {"api-key": settings.vector_db_api_key} if settings.vector_db_api_key else {}
    dsn = os.environ.get("SCOUT_INGEST_POSTGRES_DSN")
    postgres = PostgresStore(lambda: asyncpg.connect(dsn)) if dsn and not only_elasticsearch else None
    try:
        async with httpx.AsyncClient(base_url=settings.vector_db_url.rstrip("/"), headers=headers, timeout=30) as http:
            ingestor = Ingestor(
                embed=get_embedder().embed, model=settings.embedding_model,
                es=ElasticsearchStore(es, index),
                qdrant=None if only_elasticsearch else QdrantStore(http, collection),
                postgres=postgres, max_chars=max_chars, force=force)
            return await ingestor.run(documents)
    except asyncpg.PostgresError as exc:
        raise IngestError(f"postgres refused the write: {exc}") from exc
    finally:
        await es.close()
