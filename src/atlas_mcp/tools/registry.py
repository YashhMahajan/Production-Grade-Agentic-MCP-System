"""Component 4 — Tool Registry & Discovery.

Two jobs:

1. Maintain an in-memory index of available tools so ``list_tools`` and
   ``call_tool`` can resolve by name without a disk hit per request.
2. Expose a ``/.well-known/mcp-server`` endpoint that lets registries,
   crawlers, and MCP hosts discover what this server offers *without
   connecting a session first*. This is a priority in the 2026 MCP roadmap.

The discovery document is deliberately filterable by tenant and scope — a
registry scanning for public tools will see only what policy allows it to see.
"""

from __future__ import annotations

from typing import Iterable

from mcp.types import Tool as MCPToolSpec
from starlette.requests import Request
from starlette.responses import JSONResponse

from atlas_mcp.errors.framework import ToolNotFoundError
from atlas_mcp.tools.base import Tool, ToolLevel, ToolMetadata


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ── Registration / discovery ──────────────────────────────────────────
    def register(self, tool: Tool) -> None:
        name = tool.meta.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} already registered")
        self._tools[name] = tool

    async def discover(self, policy=None) -> None:
        """Load tools from the ``atlas_mcp.tools.*`` packages.

        In production you would use setuptools entry points so that third
        parties can plug in tools by installing a package. Shown here as
        explicit imports for clarity.
        """
        from atlas_mcp.auth.policy import PolicyEngine
        from atlas_mcp.tools.atomic.elasticsearch import ElasticsearchSearchTool
        from atlas_mcp.tools.atomic.http_client import HTTPFetchTool
        from atlas_mcp.tools.atomic.postgres import PostgresQueryTool
        from atlas_mcp.tools.atomic.s3_storage import S3GetTool, S3PutTool
        from atlas_mcp.tools.atomic.vector_search import VectorSearchTool
        from atlas_mcp.tools.composed.hybrid_search import HybridSearchTool
        from atlas_mcp.tools.composed.semantic_search import SemanticSearchTool
        from atlas_mcp.tools.workflow.customer_context import CustomerContextTool

        if policy is None:
            policy = PolicyEngine(rules=[], default_deny=False)

        # Atomic (Level 1)
        for cls in (
            PostgresQueryTool, ElasticsearchSearchTool, VectorSearchTool,
            S3GetTool, S3PutTool, HTTPFetchTool,
        ):
            self.register(cls(policy))
        # Composed (Level 2)
        for cls in (SemanticSearchTool, HybridSearchTool):
            self.register(cls(policy))
        # Workflow (Level 3)
        for cls in (CustomerContextTool,):
            self.register(cls(policy))

    # ── Lookup ────────────────────────────────────────────────────────────
    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(name) from None

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    # ── Filtering for list_tools ──────────────────────────────────────────
    def list_visible(self, tenant: str, scopes: Iterable[str]) -> list[MCPToolSpec]:
        """Hide tools whose required scopes the caller does not hold.

        This is a list-time filter, not a call-time authorization — policy
        still runs on every invocation. The filter exists so agents do not
        see tools they cannot use, which keeps their context window clean
        and their tool-choice accuracy high.
        """
        scope_set = set(scopes)
        visible = []
        for tool in self._tools.values():
            if not all(s in scope_set or "tool:*:admin" in scope_set for s in tool.meta.scopes_required):
                continue
            visible.append(_to_mcp_spec(tool))
        return visible

    # ── /.well-known endpoint ─────────────────────────────────────────────
    async def well_known_endpoint(self, _request: Request) -> JSONResponse:
        """Serve unauthenticated capability metadata.

        Only non-sensitive summary is exposed here: tool names, levels, and
        descriptions. Input schemas and policy details require a session.
        """
        return JSONResponse(
            {
                "protocol_version": "2025-11",
                "server": {"name": "atlas-mcp", "version": "0.1.0"},
                "capabilities": {
                    "tools": {"list_changed": True},
                    "prompts": {},
                    "resources": {},
                },
                "tools_summary": [
                    {
                        "name": t.meta.name,
                        "level": t.meta.level.value,
                        "description": t.meta.description,
                        "tags": list(t.meta.tags),
                    }
                    for t in self._tools.values()
                ],
                "authorization_server": {
                    "issuer": "https://auth.atlas.local",
                    "metadata_url": (
                        "https://auth.atlas.local/.well-known/oauth-authorization-server"
                    ),
                },
            }
        )


def _to_mcp_spec(tool: Tool) -> MCPToolSpec:
    schema = tool.input_schema.model_json_schema()
    return MCPToolSpec(
        name=tool.meta.name,
        description=tool.meta.description,
        inputSchema=schema,
    )
