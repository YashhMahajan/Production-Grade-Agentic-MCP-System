"""Shared embedding client.

One process-wide model, loaded on first use. Three tools need embeddings
(semantic_search, hybrid_search, customer.build_context) and each used to build
its own copy of the model at startup, which tripled memory and start time.

The runtime is fastembed (ONNX), not sentence-transformers, so the install does
not pull in PyTorch. The default model is BAAI/bge-base-en-v1.5; set
ATLAS_EMBEDDING_MODEL to change it. Seed data and queries must use the same
model, or vector search returns noise.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from functools import lru_cache

from atlas_mcp.config import get_settings


class EmbeddingClient:
    def __init__(self, model_name: str | None = None, cache_dir: str | None = None):
        # Imported here so the package imports without fastembed present.
        from fastembed import TextEmbedding

        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self.model = TextEmbedding(
            model_name=self.model_name, cache_dir=cache_dir or settings.embedding_cache_dir
        )

    async def embed(self, texts: Iterable[str]) -> list[list[float]]:
        items = list(texts)

        def _encode() -> list[list[float]]:
            return [vec.tolist() for vec in self.model.embed(items)]

        # Inference is CPU bound; keep it off the event loop.
        return await asyncio.to_thread(_encode)


@lru_cache(maxsize=1)
def get_embedder() -> EmbeddingClient:
    return EmbeddingClient()
