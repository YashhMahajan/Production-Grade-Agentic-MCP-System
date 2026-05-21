"""Shared embedding client.

Isolated here so composed tools do not each rewire the same HTTP calls.
Production deployments typically point this at a managed embedding endpoint
(Cohere, Voyage, Anthropic's embeddings, or a self-hosted model behind
TEI / TGI). For the demo we hit an OpenAI-compatible endpoint.
"""

from __future__ import annotations

import asyncio
import os
from typing import Iterable

import httpx

from atlas_mcp.errors.framework import UpstreamError


class EmbeddingClient:
    """Local HuggingFace embeddings client using sentence-transformers."""

    def __init__(self, model_name: str | None = None):
        # We import locally so the dependency is only required if used
        from sentence_transformers import SentenceTransformer
        
        actual_model_name = model_name or os.environ.get("ATLAS_EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
        hf_token = os.environ.get("ATLAS_HF_TOKEN")
        
        self.model = SentenceTransformer(actual_model_name, token=hf_token)

    async def embed(self, texts: Iterable[str]) -> list[list[float]]:
        # Run local inference in a thread to avoid blocking the async event loop
        def _encode():
            return self.model.encode(list(texts), normalize_embeddings=True).tolist()
        
        return await asyncio.to_thread(_encode)
