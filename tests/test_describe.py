"""`postgres.describe`: the tables and columns the caller may read, in one call.

The agent used to find column names by guessing in SQL and reading the error. This answers
from fixed SQL against the catalog, and leaves out anything policy would refuse the caller:
a table it cannot read, and the columns the PII rule hides from its role.
"""

from __future__ import annotations

import pytest

from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.tools.atomic import postgres as pg
from scout_mcp.tools.atomic.postgres import PostgresDescribeInput, PostgresDescribeTool
from scout_mcp.tools.base import CALLER
from tests.conftest import call
from tests.test_tools import FakePool

COLUMNS = [  # (table, column, type, nullable), in ordinal order
    ("customers", "id", "text", "NO"), ("customers", "name", "text", "NO"),
    ("customers", "ssn", "text", "YES"), ("customers", "dob", "date", "YES"),
    ("orders", "id", "text", "NO"), ("orders", "customer_id", "text", "NO"),
    ("orders", "total_cents", "bigint", "NO"),
]
FOREIGN_KEYS = [("orders", "customer_id", "customers", "id")]


@pytest.fixture
def catalog(monkeypatch):
    """Replace the catalog queries, and remember what was asked."""
    asked = []

    async def fetch(self, tenant, schema, tables):
        asked.append((tenant, schema, tables))
        wanted = [c for c in COLUMNS if tables is None or c[0] in tables]
        return wanted, FOREIGN_KEYS
    monkeypatch.setattr(PostgresDescribeTool, "_fetch", fetch)
    return asked


def describe(args=None, rules=None) -> PostgresDescribeTool:
    return PostgresDescribeTool(PolicyEngine(rules=rules or [], default_deny=False))


async def run_as(subjects, tool, **args):
    token = CALLER.set(tuple(subjects))
    try:
        return await tool.run("acme", PostgresDescribeInput(**args))
    finally:
        CALLER.reset(token)


# ── The answer ─────────────────────────────────────────────────────────────
async def test_tables_come_back_with_columns_types_and_foreign_keys(catalog):
    out = await run_as(["role:support_copilot"], describe())
    orders = next(t for t in out["tables"] if t["name"] == "orders")
    assert [c["name"] for c in orders["columns"]] == ["id", "customer_id", "total_cents"]
    assert orders["columns"][2] == {"name": "total_cents", "type": "bigint", "nullable": False}
    assert orders["foreign_keys"] == ["customer_id -> customers.id"]
    assert out["schema_name"] == "public"


async def test_naming_tables_asks_for_just_those(catalog):
    out = await run_as(["role:support_copilot"], describe(), tables=["orders"])
    assert [t["name"] for t in out["tables"]] == ["orders"] and catalog == [("acme", "public", ["orders"])]


async def test_a_foreign_key_to_a_table_the_caller_cannot_see_is_not_shown(catalog):
    out = await run_as(["role:support_copilot"], describe(), tables=["orders"])
    # customers was not asked for, so the reference would name a table the answer leaves out.
    assert out["tables"][0]["foreign_keys"] == []


# ── Policy ─────────────────────────────────────────────────────────────────
@pytest.fixture
def real_policy():
    return PolicyEngine.from_file("config/policy.yaml")


async def test_columns_the_pii_rule_hides_from_the_role_are_left_out_and_counted(catalog, real_policy):
    out = await run_as(["agent:x", "role:support_copilot"], PostgresDescribeTool(real_policy))
    customers = next(t for t in out["tables"] if t["name"] == "customers")
    assert [c["name"] for c in customers["columns"]] == ["id", "name"]
    assert customers["hidden_columns"] == 2
    assert "ssn" not in str(out) and "dob" not in str(out)  # not even the names


async def test_a_role_with_no_rule_for_a_table_does_not_see_the_table(catalog, real_policy):
    out = await run_as(["user:lee", "role:analyst"], PostgresDescribeTool(real_policy))
    assert out["tables"] == []  # the analyst role may only read the analytics schema


async def test_the_analyst_sees_the_analytics_schema(catalog, real_policy, monkeypatch):
    out = await run_as(["user:lee", "role:analyst"], PostgresDescribeTool(real_policy),
                       schema="analytics", tables=["orders"])
    assert [t["name"] for t in out["tables"]] == ["orders"] and out["schema_name"] == "analytics"


async def test_with_no_caller_identity_nothing_is_described(catalog, real_policy):
    out = await PostgresDescribeTool(real_policy).run("acme", PostgresDescribeInput())
    assert out["tables"] == []  # fails closed


async def test_the_result_is_never_cached_because_it_depends_on_who_asks():
    assert PostgresDescribeTool.meta.cacheable is False


# ── Input and SQL ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    {"tables": ["orders; DROP TABLE x"]}, {"tables": ['"orders"']}, {"schema": "public; --"},
    {"tables": ["a" * 64]}, {"tables": []}, {"tables": ["t"] * 51},
    {"schema": "pg_catalog"}, {"schema": "PG_TEMP_1"}, {"schema": "information_schema"},
])
def test_names_that_are_not_plain_identifiers_are_refused(bad):
    with pytest.raises(ValueError):
        PostgresDescribeInput(**bad)


async def test_the_sql_is_fixed_names_travel_as_parameters_inside_a_read_only_transaction():
    tool = describe()
    pool = FakePool([])
    pool.conn.rows = []

    async def ensure():
        return pool
    tool._pg._ensure_pool = ensure

    async def fetch(sql, *params):
        pool.conn.log.append(("fetch", sql, params))
        return []
    pool.conn.fetch = fetch
    await tool._fetch("acme", "public", ["orders"])
    log = pool.conn.log
    assert log[0] == ("begin", True) and log[1][0] == "execute" and log[1][2] == ("acme",)
    sqls = [e[1] for e in log if e[0] == "fetch"]
    assert sqls == [pg.DESCRIBE_COLUMNS_SQL, pg.DESCRIBE_FOREIGN_KEYS_SQL]
    assert [e[2] for e in log if e[0] == "fetch"][0] == ("public", ["orders"])
    assert "orders" not in "".join(sqls)


# ── Through the server ─────────────────────────────────────────────────────
async def test_through_the_server_the_caller_roles_decide_what_is_shown(app_client, catalog):
    is_error, out = await call(app_client, "postgres.describe", {}, token="support")
    assert not is_error
    customers = next(t for t in out["tables"] if t["name"] == "customers")
    assert [c["name"] for c in customers["columns"]] == ["id", "name"] and customers["hidden_columns"] == 2

    # The analyst role may read only the analytics schema, so describing public is refused outright.
    is_error, out = await call(app_client, "postgres.describe", {}, token="analyst")
    assert is_error and out["code"] == "not_authorized"
    is_error, out = await call(app_client, "postgres.describe", {"schema": "analytics", "tables": ["orders"]}, token="analyst")
    assert not is_error and [t["name"] for t in out["tables"]] == ["orders"]


async def test_the_agent_may_call_it_and_is_told_to(app_client):
    from scout_mcp.agents import retriever
    from scout_mcp.agents.prompts import RETRIEVER_SYSTEM
    assert "postgres.describe" in retriever._ALLOWED_TOOLS and "postgres.describe" in RETRIEVER_SYSTEM
