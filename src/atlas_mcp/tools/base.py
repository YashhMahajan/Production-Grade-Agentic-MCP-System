"""Component 6 — Tool Execution Engine.

Every tool in Atlas-MCP is an instance of :class:`Tool`. The production
lesson baked into this design is the **three-level hierarchy**:

* **Atomic** tools wrap a single primitive operation on a single backend.
  ``postgres.query`` and ``s3.get_object`` are atomic.
* **Composed** tools chain a small, fixed set of atomic tools behind one
  higher-level name. ``semantic_search`` is composed: vector lookup, rerank,
  fetch bodies.
* **Workflow** tools wrap a multi-step agentic procedure. ``research_topic``
  is a workflow: plan, fan out, synthesise, cite.

Agents pick the right granularity; the server does not force them into the
atomic layer if a composed tool already encodes the common intent. The April
2026 production report on MCP servers with 87+ tools found this hierarchy
cuts the erroneous-call rate by around 40% because agents stop constructing
brittle multi-step plans from atomic primitives.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel

from atlas_mcp.auth.policy import PolicyEngine
from atlas_mcp.errors.framework import ValidationError


class ToolLevel(str, Enum):
    ATOMIC = "atomic"
    COMPOSED = "composed"
    WORKFLOW = "workflow"


@dataclass
class ToolMetadata:
    """Descriptive metadata surfaced via list_tools and /.well-known."""

    name: str
    description: str
    level: ToolLevel
    scopes_required: tuple[str, ...] = ()
    destructive: bool = False
    cacheable: bool = True
    cache_ttl_seconds: int = 60
    timeout_ms: int = 10_000
    tags: tuple[str, ...] = ()


class Tool(ABC):
    """Base class. Subclass, declare a schema, implement ``run``."""

    # Subclasses MUST override these class attributes.
    meta: ClassVar[ToolMetadata]
    input_schema: ClassVar[type[BaseModel]]

    def __init__(self, policy: PolicyEngine):
        self.policy = policy

    # ── Public surface used by the dispatch pipeline ───────────────────────
    def validate(self, arguments: dict) -> BaseModel:
        """Component 5 entry point. Raises :class:`ValidationError` on bad input."""
        try:
            return self.input_schema.model_validate(arguments)
        except Exception as exc:
            raise ValidationError(
                code="invalid_arguments",
                retryable=False,
                hint=_summarise_pydantic_error(exc),
                context={"tool": self.meta.name},
            ) from exc

    def cache_key(self, tenant: str, args: BaseModel) -> str:
        """Deterministic hash used by the Component 9 cache layer."""
        payload = {"tool": self.meta.name, "tenant": tenant, "args": args.model_dump(mode="json")}
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return f"atlas:{self.meta.name}:{hashlib.sha256(blob).hexdigest()[:24]}"

    @property
    def cacheable(self) -> bool:
        return self.meta.cacheable

    @property
    def cache_ttl_seconds(self) -> int:
        return self.meta.cache_ttl_seconds

    async def execute(self, tenant: str, args: BaseModel) -> dict:
        """Called by the dispatch pipeline after all guards have passed."""
        return await self.run(tenant, args)

    # ── Subclasses implement this ──────────────────────────────────────────
    @abstractmethod
    async def run(self, tenant: str, args: BaseModel) -> dict:
        """The actual work. Raise :class:`UpstreamError` for backend failures."""
        ...


def _summarise_pydantic_error(exc: Exception) -> str:
    """Condense pydantic errors into one agent-readable line."""
    if hasattr(exc, "errors"):
        errs = exc.errors()  # type: ignore[attr-defined]
        if errs:
            first = errs[0]
            loc = ".".join(str(p) for p in first.get("loc", []))
            return f"{loc}: {first.get('msg', 'invalid')}"
    return str(exc)
