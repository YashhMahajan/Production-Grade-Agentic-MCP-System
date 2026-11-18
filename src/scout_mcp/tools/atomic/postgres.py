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
from typing import Annotated, Any, ClassVar

import asyncpg
from pydantic import BaseModel, Field, StringConstraints, field_validator

from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.config import get_settings
from scout_mcp.errors.framework import UpstreamError
from scout_mcp.tenant_sql import set_tenant_sql
from scout_mcp.tools.base import CALLER, Tool, ToolLevel, ToolMetadata

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


class PostgresQueryOutput(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


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
    output_schema: ClassVar[type[BaseModel] | None] = PostgresQueryOutput

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
                # filter cannot write. SET LOCAL is transaction-local, and a SELECT cannot
                # issue it. set_config() is refused to this role by the database itself.
                async with conn.transaction(readonly=True):
                    await conn.execute(set_tenant_sql(tenant))
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


# ── postgres.describe ─────────────────────────────────────────────────────
# Fixed SQL: names travel as bind parameters, never inside the text. The columns come from
# information_schema, which lists only tables this role can read. Foreign keys come from the
# catalog because information_schema hides constraints on tables the role does not own.
DESCRIBE_COLUMNS_SQL = (
    "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
    "WHERE table_schema = $1 AND ($2::text[] IS NULL OR table_name = ANY($2)) "
    "ORDER BY table_name, ordinal_position"
)
DESCRIBE_FOREIGN_KEYS_SQL = (
    "SELECT t.relname AS table_name, a.attname AS column_name, "
    "rt.relname AS ref_table, ra.attname AS ref_column "
    "FROM pg_constraint c "
    "JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "JOIN pg_class rt ON rt.oid = c.confrelid "
    "CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS k(attnum, fattnum) "
    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
    "JOIN pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = k.fattnum "
    "WHERE c.contype = 'f' AND n.nspname = $1 ORDER BY t.relname, a.attname"
)

_IDENTIFIER = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"


class PostgresDescribeInput(BaseModel):
    schema_name: str = Field("public", alias="schema", pattern=_IDENTIFIER,
                             description="Postgres schema to describe.")
    tables: list[Annotated[str, StringConstraints(pattern=_IDENTIFIER)]] | None = Field(
        None, min_length=1, max_length=50, description="Only these tables. Leave out for all you may read.")


    @field_validator("schema_name")
    @classmethod
    def not_a_system_schema(cls, value: str) -> str:
        # postgres.query refuses every pg_* name and the catalog views; describing them would
        # list the same names it was built to keep an agent away from.
        if value.lower().startswith("pg_") or value.lower() == "information_schema":
            raise ValueError("system schemas cannot be described")
        return value


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool


class TableInfo(BaseModel):
    name: str
    columns: list[ColumnInfo]
    # How many columns exist that this caller may not read. Their names are not given.
    hidden_columns: int = 0
    foreign_keys: list[str]  # "customer_id -> customers.id"


class PostgresDescribeOutput(BaseModel):
    schema_name: str
    tables: list[TableInfo]


class PostgresDescribeTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="postgres.describe",
        description=(
            "List the tables and columns you may read, with types and foreign keys. "
            "Call this before writing SQL for postgres.query instead of guessing column names."
        ),
        level=ToolLevel.ATOMIC,
        scopes_required=("tool:postgres:read",),
        # The answer depends on who asks (hidden tables and columns), and the cache key does not
        # include the caller: a cached full answer could be served to a role that may not see it.
        cacheable=False,
        timeout_ms=5_000,
        tags=("postgres", "schema", "read"),
    )
    input_schema: ClassVar[type[BaseModel]] = PostgresDescribeInput
    output_schema: ClassVar[type[BaseModel] | None] = PostgresDescribeOutput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self._pg = PostgresQueryTool(policy)  # shares its connection pool

    def policy_resource(self, args: PostgresDescribeInput) -> str:  # type: ignore[override]
        # The same names postgres.query uses: bare for public, "schema/table" otherwise.
        return "*" if args.schema_name == "public" else f"{args.schema_name}/*"

    async def _fetch(self, tenant: str, schema: str, tables: list[str] | None):
        pool = await self._pg._ensure_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction(readonly=True):
                    await conn.execute(set_tenant_sql(tenant))
                    columns = await conn.fetch(DESCRIBE_COLUMNS_SQL, schema, tables)
                    keys = await conn.fetch(DESCRIBE_FOREIGN_KEYS_SQL, schema)
        except asyncpg.PostgresError as exc:
            raise UpstreamError("postgres_error", retryable=False, hint=str(exc).splitlines()[0],
                                context={"sqlstate": getattr(exc, "sqlstate", None)}) from exc
        return (
            [(r["table_name"], r["column_name"], r["data_type"], r["is_nullable"]) for r in columns],
            [(r["table_name"], r["column_name"], r["ref_table"], r["ref_column"]) for r in keys],
        )

    def _may_read(self, subjects: tuple[str, ...], tenant: str, resource: str, column: str) -> bool:
        context = {"sql": "", "columns": [column.lower()]}
        return self.policy.decide(subjects, tenant, "tool:postgres:read", resource, context).allowed

    async def run(self, tenant: str, args: PostgresDescribeInput) -> dict:  # type: ignore[override]
        subjects = CALLER.get()  # empty means nobody, and policy refuses nobody
        schema = args.schema_name
        columns, keys = await self._fetch(tenant, schema, args.tables)

        by_table: dict[str, list[tuple[str, str, str]]] = {}
        for table, column, type_, nullable in columns:
            by_table.setdefault(table, []).append((column, type_, nullable))

        shown: dict[str, dict] = {}
        for table, rows in by_table.items():
            resource = table if schema == "public" else f"{schema}/{table}"
            # A table the caller cannot read has every column refused, so it is left out below.
            visible = [ColumnInfo(name=c, type=t, nullable=n == "YES").model_dump()
                       for c, t, n in rows if self._may_read(subjects, tenant, resource, c)]
            if visible:
                shown[table] = {"name": table, "columns": visible,
                                "hidden_columns": len(rows) - len(visible), "foreign_keys": []}

        names = {t: {c["name"] for c in s["columns"]} for t, s in shown.items()}
        for table, column, ref_table, ref_column in keys:
            # Only a reference between two things the caller can see: it must not name a hidden table or column.
            if column in names.get(table, ()) and ref_column in names.get(ref_table, ()):
                shown[table]["foreign_keys"].append(f"{column} -> {ref_table}.{ref_column}")
        return {"schema_name": schema, "tables": list(shown.values())}
