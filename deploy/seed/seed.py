"""Load demo data into Elasticsearch and Qdrant.

Postgres is seeded by deploy/sql/init.sql. This script covers the other two
stores so the copilot has tickets and documents to find:

* Elasticsearch: `tickets` and `documents` indices. Every doc carries `_tenant`,
  which the search tool filters on.
* Qdrant: `support_docs`, one point per document. Qdrant ids must be integers or
  UUIDs, so the point id is a UUID derived from the document id and the original
  id is stored in the payload as `doc_id`.

Run it inside the atlas-mcp image so it embeds with the same model as the server:

    docker compose run --rm seed

It is idempotent: indices and the collection are recreated each run.
"""

from __future__ import annotations

import os
import uuid

import httpx
from elasticsearch import Elasticsearch
from fastembed import TextEmbedding

ES_URL = os.environ.get("ATLAS_ELASTICSEARCH_URL", "http://localhost:9200")
ES_USER = os.environ.get("ATLAS_ELASTICSEARCH_USER", "elastic")
ES_PASSWORD = os.environ.get("ATLAS_ELASTICSEARCH_PASSWORD")
QDRANT_KEY = os.environ.get("ATLAS_VECTOR_DB_API_KEY")
QDRANT_URL = os.environ.get("ATLAS_VECTOR_DB_URL", "http://localhost:6333").rstrip("/")
MODEL = os.environ.get("ATLAS_EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
COLLECTION = "support_docs"

# Same rows as the `documents` table in init.sql.
DOCUMENTS = [
    {"id": "doc_refund_policy", "tenant": "acme", "title": "Refund policy",
     "body": "Refunds are issued within 5 business days of receiving the returned item."},
    {"id": "doc_shipping", "tenant": "acme", "title": "Shipping timelines",
     "body": "Standard shipping is 3-5 business days. Express is 1-2."},
    {"id": "doc_globex_sla", "tenant": "globex", "title": "Support SLA",
     "body": "Gold customers get a first response within 2 hours."},
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


def point_id(doc_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atlas/{doc_id}"))


def seed_elasticsearch() -> None:
    es = Elasticsearch(ES_URL, basic_auth=(ES_USER, ES_PASSWORD) if ES_PASSWORD else None)
    for index, rows in (("tickets", TICKETS), ("documents", DOCUMENTS)):
        es.options(ignore_status=404).indices.delete(index=index)
        es.indices.create(index=index)
        for row in rows:
            doc = {k: v for k, v in row.items() if k not in ("id", "tenant")}
            es.index(index=index, id=row["id"], document={**doc, "_tenant": row["tenant"]})
        es.indices.refresh(index=index)
        print(f"elasticsearch: {index} <- {len(rows)} docs")


def seed_qdrant() -> None:
    # The image has the model baked into this directory, and the seed container has no
    # route to the internet, so it must be read from there, never downloaded.
    model = TextEmbedding(model_name=MODEL, cache_dir=os.environ.get("ATLAS_EMBEDDING_CACHE_DIR"))
    texts = [f'{d["title"]}. {d["body"]}' for d in DOCUMENTS]
    vectors = [v.tolist() for v in model.embed(texts)]
    dim = len(vectors[0])
    headers = {"api-key": QDRANT_KEY} if QDRANT_KEY else {}
    with httpx.Client(timeout=30, headers=headers) as http:
        http.delete(f"{QDRANT_URL}/collections/{COLLECTION}")
        http.put(
            f"{QDRANT_URL}/collections/{COLLECTION}",
            json={"vectors": {"size": dim, "distance": "Cosine"}},
        ).raise_for_status()
        points = [
            {
                "id": point_id(d["id"]),
                "vector": vec,
                "payload": {"tenant": d["tenant"], "doc_id": d["id"], "title": d["title"],
                            "text": d["body"]},
            }
            for d, vec in zip(DOCUMENTS, vectors)
        ]
        http.put(
            f"{QDRANT_URL}/collections/{COLLECTION}/points", params={"wait": "true"},
            json={"points": points},
        ).raise_for_status()
    print(f"qdrant: {COLLECTION} <- {len(points)} points ({dim} dims, model {MODEL})")


if __name__ == "__main__":
    seed_elasticsearch()
    seed_qdrant()
