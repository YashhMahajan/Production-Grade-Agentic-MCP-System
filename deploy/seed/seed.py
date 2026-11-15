"""Load demo data into Elasticsearch, Qdrant and Postgres.

* Tickets go straight into Elasticsearch (`tickets`). Every doc carries `_tenant`, which the
  search tool filters on.
* Documents go through the same library as `scout-mcp ingest`: chunked, embedded with the
  server's model, and written to Elasticsearch (`documents`), Qdrant (`support_docs`) and,
  when SCOUT_INGEST_POSTGRES_DSN is set, the Postgres `documents` table.

Run it inside the scout-mcp image so it embeds with the same model as the server:

    docker compose --profile seed run --rm seed

It is idempotent: indices and the collection are recreated each run.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from elasticsearch import Elasticsearch

from scout_mcp.ingest import Document, ingest_documents

ES_URL = os.environ.get("SCOUT_ELASTICSEARCH_URL", "http://localhost:9200")
ES_USER = os.environ.get("SCOUT_ELASTICSEARCH_USER", "elastic")
ES_PASSWORD = os.environ.get("SCOUT_ELASTICSEARCH_PASSWORD")
QDRANT_KEY = os.environ.get("SCOUT_VECTOR_DB_API_KEY")
QDRANT_URL = os.environ.get("SCOUT_VECTOR_DB_URL", "http://localhost:6333").rstrip("/")
COLLECTION = "support_docs"

# Same rows as the `documents` table in init.sql.
DOCUMENTS = [
    Document("doc_refund_policy", "acme", "Refund policy",
             "Refunds are issued within 5 business days of receiving the returned item."),
    Document("doc_shipping", "acme", "Shipping timelines",
             "Standard shipping is 3-5 business days. Express is 1-2."),
    Document("doc_globex_sla", "globex", "Support SLA",
             "Gold customers get a first response within 2 hours."),
]

# Exact-match fields must be `keyword`. With Elasticsearch's default dynamic mapping they
# become analysed text: "CUST-1001" is split into tokens so a term query never matches it.
# The `documents` mapping, with the same rule for `_tenant`, is DOCUMENT_MAPPINGS in scout_mcp.ingest.
TICKET_MAPPING = {"properties": {
    "customer_id": {"type": "keyword"}, "status": {"type": "keyword"},
    "priority": {"type": "keyword"}, "subject": {"type": "text"},
    "created_at": {"type": "date"}, "_tenant": {"type": "keyword"}}}

# Searchable text from a tenant whose name starts with another tenant's name. It is in
# Elasticsearch only; the end-to-end tests check that "acme" never sees it.
LOOKALIKE_DOCS = [
    Document("doc_acmecorp_refunds", "acme-corp", "Refund policy",
             "Refunds are issued within 5 business days of receiving the returned item."),
]

TICKETS = [
    {"id": "t_5001", "tenant": "acme", "customer_id": "CUST-1001", "status": "open",
     "priority": "high", "subject": "Refund for order o_9002 has not arrived",
     "created_at": "2026-05-12T09:14:00Z"},
    {"id": "t_5002", "tenant": "acme", "customer_id": "CUST-1002", "status": "pending",
     "priority": "normal", "subject": "Where is order o_9003?",
     "created_at": "2026-05-13T16:40:00Z"},
    {"id": "t_6001", "tenant": "globex", "customer_id": "CUST-2001", "status": "open",
     "priority": "normal", "subject": "Invoice address change",
     "created_at": "2026-05-14T11:05:00Z"},
]


def seed_elasticsearch() -> None:
    es = Elasticsearch(ES_URL, basic_auth=(ES_USER, ES_PASSWORD) if ES_PASSWORD else None)
    for index in ("tickets", "documents"):
        es.options(ignore_status=404).indices.delete(index=index)
    es.indices.create(index="tickets", mappings=TICKET_MAPPING)
    for row in TICKETS:
        doc = {k: v for k, v in row.items() if k not in ("id", "tenant")}
        es.index(index="tickets", id=row["id"], document={**doc, "_tenant": row["tenant"]})
    es.indices.refresh(index="tickets")
    print(f"elasticsearch: tickets <- {len(TICKETS)} docs")


def reset_qdrant() -> None:
    headers = {"api-key": QDRANT_KEY} if QDRANT_KEY else {}
    httpx.delete(f"{QDRANT_URL}/collections/{COLLECTION}", headers=headers, timeout=30)


async def seed_documents() -> None:
    report = await ingest_documents(DOCUMENTS, collection=COLLECTION)
    print(f"documents: {report.documents} written, {report.chunks} chunks embedded")
    # The lookalike tenant is in Elasticsearch only, so the tests can show "acme" never sees it.
    await ingest_documents(LOOKALIKE_DOCS, only_elasticsearch=True)
    es = Elasticsearch(ES_URL, basic_auth=(ES_USER, ES_PASSWORD) if ES_PASSWORD else None)
    es.indices.refresh(index="documents")


if __name__ == "__main__":
    seed_elasticsearch()
    reset_qdrant()
    asyncio.run(seed_documents())
