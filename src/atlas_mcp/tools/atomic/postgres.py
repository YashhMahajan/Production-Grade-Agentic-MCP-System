"""Atomic Postgres query tool — Level 1 of the three-level hierarchy.

Exposes a narrowly-scoped read-only interface over Postgres. The tool
*refuses* to execute anything other than a SELECT. Write operations live in
their own tool with destructive=True and human-in-the-loop approval.

Why separate read and write: agents routinely attempt to combine them when
given an unrestricted SQL surface. Splitting them turns a prompt injection
into a policy rejection instead of a dropped table.
"""

from __future__ import annotations

from typing import Any, ClassVar

import asyncpg
from pydantic import BaseModel, Field, field_validator

from atlas_mcp.auth.policy import PolicyEngine
from atlas_mcp.config import get_settings
from atlas_mcp.errors.framework import UpstreamError, ValidationError
from atlas_mcp.tools.base import Tool, ToolLevel, ToolMetadata


_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
    "CREATE", "GRANT", "REVOKE", "COPY", "CALL", "EXECUTE",
)


class PostgresQueryInput(BaseModel):
    sql: str = Field(..., description="A single SELECT statement.")
    params: list[Any] = Field(default_factory=list, description="Positional parameters.")
    max_rows: int = Field(100, ge=1, le=1000)

    @field_validator("sql")
    @classmethod
    def must_be_select(cls, value: str) -> str:
        stripped = value.strip().rstrip(";")
        if not stripped:
            raise ValueError("sql is required")
        head = stripped.split(None, 1)[0].upper()
        if head not in {"SELECT", "WITH"}:
            raise ValueError("only SELECT or WITH statements are permitted")
        upper = stripped.upper()
        for kw in _FORBIDDEN_KEYWORDS:
            # Word-boundary check: "UPDATE_TS" is allowed, "UPDATE " is not.
            if f" {kw} " in f" {upper} " or upper.startswith(f"{kw} "):
                raise ValueError(f"forbidden keyword: {kw}")
        # Reject multiple statements.
        if ";" in stripped:
            raise ValueError("only one statement per call")
        return stripped


class PostgresQueryTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="postgres.query",
        description="Execute a single read-only SELECT against the Postgres warehouse.",
        level=ToolLevel.ATOMIC,
        scopes_required=("tool:postgres:read",),
        destructive=False,
        cacheable=True,
        cache_ttl_seconds=30,
        timeout_ms=5_000,
        tags=("postgres", "sql", "read"),
    )
    input_schema: ClassVar[type[BaseModel]] = PostgresQueryInput

    _pool: asyncpg.Pool | None = None

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.settings = get_settings()

    async def _ensure_pool(self) -> asyncpg.Pool:
        if PostgresQueryTool._pool is None:
            PostgresQueryTool._pool = await asyncpg.create_pool(
                self.settings.postgres_dsn,
                min_size=2,
                max_size=10,
                command_timeout=self.meta.timeout_ms / 1000,
            )
        return PostgresQueryTool._pool

    async def run(self, tenant: str, args: PostgresQueryInput) -> dict:  # type: ignore[override]
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                # Tenant isolation is enforced at the row-security level in Postgres.
                # We set the tenant id on the connection before every query.
                await conn.execute("SET LOCAL app.tenant_id = $1", tenant)
                rows = await conn.fetch(
                    f"{args.sql} LIMIT {args.max_rows}", *args.params
                )
        except asyncpg.PostgresError as exc:
            # Distinguish deterministic query errors from transient ones.
            retryable = isinstance(exc, (asyncpg.ConnectionDoesNotExistError,
                                         asyncpg.InterfaceError,
                                         asyncpg.TooManyConnectionsError))
            raise UpstreamError(
                code="postgres_error",
                retryable=retryable,
                hint=str(exc).splitlines()[0],
                context={"sqlstate": getattr(exc, "sqlstate", None)},
            ) from exc

        return {
            "columns": list(rows[0].keys()) if rows else [],
            "rows": [dict(r) for r in rows],
            "row_count": len(rows),
            "truncated": len(rows) >= args.max_rows,
        }
