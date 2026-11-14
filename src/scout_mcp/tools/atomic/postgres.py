"""Atomic Postgres query tool — Level 1 of the three-level hierarchy.

Exposes a narrowly-scoped read-only interface over Postgres. The tool
*refuses* to execute anything other than a SELECT. Write operations live in
their own tool with destructive=True and human-in-the-loop approval.

Why separate read and write: agents routinely attempt to combine them when
given an unrestricted SQL surface. Splitting them turns a prompt injection
into a policy rejection instead of a dropped table.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

import asyncpg
from pydantic import BaseModel, Field, field_validator

from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.config import get_settings
from scout_mcp.errors.framework import UpstreamError
from scout_mcp.tools.base import Tool, ToolLevel, ToolMetadata

# The forbidden keywords list is our defense-in-depth against prompt injection. Even if the policy engine has a bug, this list makes it impossible for any SQL containing DROP to execute through this tool.

_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
    "CREATE", "GRANT", "REVOKE", "COPY", "CALL", "EXECUTE",
)

# The tenant is a session setting that the row-level-security policies read. A query
# that can call set_config() could point it at another tenant, so these are refused,
# along with syntax that hides an identifier from this check: quoted identifiers,
# dollar quoting and comments.
_UNSAFE_FUNCTIONS = re.compile(
    r"\b(set_config|current_setting|pg_\w+|dblink\w*|lo_\w+|\w+_to_xml\w*|nextval|setval)\b",
    re.IGNORECASE,
)
_HIDING_SYNTAX = re.compile(r'"|--|/\*|\$[A-Za-z_]*\$')
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b")
_TABLE_RE = re.compile(r"\bFROM\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z_][a-z0-9_]*")

# Pydantic schema that validates inputs
class PostgresQueryInput(BaseModel):
    sql: str = Field(..., description="A single SELECT statement.")
    params: list[Any] = Field(default_factory=list, description="Positional parameters.")
    max_rows: int = Field(100, ge=1, le=1000) #capped with 1000 rows so memory is not exhausted.

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
        # Word boundaries, so "UPDATE_TS" and "created_at" pass but "UPDATE" does not, and a
        # newline or tab in front of the keyword does not hide it.
        if m := _FORBIDDEN_RE.search(upper):
            raise ValueError(f"forbidden keyword: {m.group(1)}")
        if m := _UNSAFE_FUNCTIONS.search(stripped):
            raise ValueError(f"function not permitted: {m.group(1)}")
        if _HIDING_SYNTAX.search(stripped):
            raise ValueError(
                "quoted identifiers, dollar quoting and comments are not permitted"
            )
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
        returns_untrusted_data=True,
    )
    input_schema: ClassVar[type[BaseModel]] = PostgresQueryInput

    # The _pool is a class variable, not an instance variable. 
    # This is deliberate. We want a single shared connection pool across all PostgresQueryTool instances because opening a new pool per instance would blow up our Postgres connection limit.
    _pool: asyncpg.Pool | None = None
    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.settings = get_settings()

    def policy_resource(self, args: PostgresQueryInput) -> str:  # type: ignore[override]
        # "analytics.orders" becomes "analytics/orders" so policy resources read like paths.
        m = _TABLE_RE.search(args.sql)
        return m.group(1).replace(".", "/") if m else "*"

    def policy_context(self, args: PostgresQueryInput) -> dict:  # type: ignore[override]
        # Every identifier in the query counts as a requested column for the PII rule.
        # That over-matches (a table named ssn would trip it), which is the safe side.
        return {"sql": args.sql, "columns": _WORD_RE.findall(args.sql.lower())}

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
                # Read-only transaction: even a SELECT that slips past the keyword
                # filter cannot write. set_config(..., true) is the transaction-local
                # form of SET; SET itself cannot take a bind parameter.
                async with conn.transaction(readonly=True):
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant)
                    cursor = await conn.cursor(args.sql, *args.params)
                    # One extra row tells us whether the result was cut off.
                    rows = await cursor.fetch(args.max_rows + 1)
        except asyncpg.PostgresError as exc:
            # Distinguish deterministic query errors from temporary or brief ones.
            retryable = isinstance(exc, (asyncpg.ConnectionDoesNotExistError,
                                         asyncpg.InterfaceError,
                                         asyncpg.TooManyConnectionsError))
            raise UpstreamError(
                code="postgres_error",
                retryable=retryable,
                hint=str(exc).splitlines()[0],
                context={"sqlstate": getattr(exc, "sqlstate", None)},
            ) from exc

        truncated = len(rows) > args.max_rows
        rows = rows[: args.max_rows]
        return {
            "columns": list(rows[0].keys()) if rows else [],
            "rows": [dict(r) for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
        }
