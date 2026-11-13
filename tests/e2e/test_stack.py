"""What an engineer would check by hand after `docker compose up`, as tests."""

from __future__ import annotations

import base64
import json
import socket
import time

import httpx
import pytest

from tests.e2e.conftest import MCP_URL, compose_exec, mcp, token, value

# ── Identity ──────────────────────────────────────────────────────────────


async def test_keycloak_tokens_carry_what_atlas_checks():
    raw = await token("copilot-acme")
    payload = raw.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["iss"] == "http://keycloak:8080/realms/atlas"  # same from host or network
    assert claims["aud"] == "atlas-mcp" or "atlas-mcp" in claims["aud"]
    assert claims["tenant"] == "acme" and claims["roles"] == "role:support_copilot"
    assert "tool:postgres:read" in claims["scope"].split()
    assert "tool:s3:write" not in claims["scope"].split()
    assert claims["exp"] - claims["iat"] == 900


async def test_requests_without_a_valid_token_are_rejected(http):
    for headers in ({}, {"Authorization": "Bearer not-a-jwt"}):
        r = http.post(f"{MCP_URL}/mcp", json={}, headers=headers)
        assert r.status_code == 401 and "WWW-Authenticate" in r.headers


async def test_a_token_for_another_audience_is_rejected(http):
    # Keycloak's own admin client token is valid but not minted for atlas-mcp.
    r = httpx.post(
        "http://localhost:8081/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli",
              "username": "admin", "password": "admin"})
    if r.status_code != 200:
        pytest.skip("admin-cli token unavailable")
    resp = http.post(f"{MCP_URL}/mcp", json={},
                     headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert resp.status_code == 401


# ── Edge ──────────────────────────────────────────────────────────────────


def test_edge_forwards_only_public_paths_and_sets_security_headers(http):
    ok = http.get(f"{MCP_URL}/healthz")
    assert ok.status_code == 200
    assert "max-age" in ok.headers["strict-transport-security"]
    assert ok.headers["x-content-type-options"] == "nosniff"
    assert "server" not in ok.headers
    for path in ("/metrics", "/readyz", "/admin", "/"):
        assert http.get(f"{MCP_URL}{path}").status_code == 404, path
    doc = http.get(f"{MCP_URL}/.well-known/mcp-server").json()
    assert doc["authorization_server"]["issuer"] == "http://keycloak:8080/realms/atlas"
    assert len(doc["tools_summary"]) == 9


async def test_oversized_request_bodies_are_refused(http):
    # With a valid token, so the request reaches the point where the body is read.
    bearer = await token("copilot-acme")
    r = http.post(f"{MCP_URL}/mcp", content=b"x" * (2 * 1024 * 1024),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
    assert r.status_code == 413


@pytest.mark.parametrize("port", [5432, 6379, 9200, 6333, 8333, 9333, 8080])
def test_stores_and_the_app_are_not_reachable_from_the_host(port):
    with socket.socket() as s:
        s.settimeout(2)
        assert s.connect_ex(("127.0.0.1", port)) != 0, f"port {port} is published"


def test_every_store_demands_credentials():
    es = compose_exec("atlas-mcp", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                      "http://elasticsearch:9200/_cluster/health")
    assert es.strip() == "401"
    qd = compose_exec("atlas-mcp", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                      "http://qdrant:6333/collections")
    assert qd.strip() in ("401", "403")
    redis = compose_exec("atlas-mcp", "python", "-c",
                         "import redis;redis.Redis(host='redis').ping()")
    assert "AuthenticationError" in redis  # redis-py words it "HELLO must be called with the client already authenticated"
    pg = compose_exec("postgres", "psql", "-h", "127.0.0.1", "-U", "atlas_app", "-d", "atlas",
                      "-tAc", "select 1", env={"PGPASSWORD": "wrong"})
    assert "authentication failed" in pg


def test_server_container_is_hardened():
    out = compose_exec("atlas-mcp", "sh", "-c",
                       "id -u; touch /app/x 2>&1; grep -E 'CapEff' /proc/self/status")
    assert out.splitlines()[0] == "10001"            # not root
    assert "Read-only file system" in out             # root filesystem is read-only
    assert "0000000000000000" in out                  # no effective capabilities


# ── Tools through the whole stack ─────────────────────────────────────────


async def test_each_client_sees_only_its_own_tools(acme):
    names = {t["name"] for t in await acme.list_tools()}
    assert {"postgres.query", "semantic_search", "customer.build_context"} <= names
    assert "s3.put_object" not in names


async def test_customer_context_joins_all_four_backends(acme):
    ctx = value(await acme.call_tool("customer.build_context", {
        "customer_id": "CUST-1001", "question": "Why is my refund late?"}))
    assert "partial_errors" not in ctx, ctx.get("partial_errors")
    assert ctx["profile"]["name"] == "Alicia Rivera"                      # Postgres
    assert {o["id"] for o in ctx["orders"]} >= {"o_9001", "o_9002"}       # Postgres
    assert [t["id"] for t in ctx["tickets"]] == ["t_5001"]                # Elasticsearch
    assert ctx["docs"][0]["id"] == "doc_refund_policy"                    # Qdrant, bge-base


async def test_hybrid_search_fuses_elasticsearch_and_qdrant(acme):
    out = value(await acme.call_tool("hybrid_search", {
        "query": "refund business days", "es_index": "documents",
        "vector_collection": "support_docs", "top_k": 3}))
    top = out["results"][0]
    assert top["id"] == "doc_refund_policy"
    assert top["lexical_rank"] == 1 and top["dense_rank"] == 1


async def test_tenants_cannot_see_each_others_data(acme, globex):
    ctx = value(await globex.call_tool("customer.build_context", {
        "customer_id": "CUST-1001", "question": "refund policy"}))
    assert ctx["profile"] == {}          # Postgres row-level security
    assert ctx["orders"] == []
    assert ctx["tickets"] == []          # Elasticsearch tenant filter
    assert {d["id"] for d in ctx["docs"]} == {"doc_globex_sla"}   # Qdrant tenant filter

    mine = value(await acme.call_tool("postgres.query", {"sql": "SELECT id FROM customers ORDER BY id"}))
    theirs = value(await globex.call_tool("postgres.query", {"sql": "SELECT id FROM customers ORDER BY id"}))
    assert [r["id"] for r in mine["rows"]] == ["CUST-1001", "CUST-1002"]
    assert [r["id"] for r in theirs["rows"]] == ["CUST-2001"]


async def test_tenant_header_cannot_override_the_token():
    bearer = await token("copilot-acme")
    with httpx.Client(verify=False) as c:
        r = c.post(f"{MCP_URL}/mcp", json={}, headers={
            "Authorization": f"Bearer {bearer}", "X-Tenant-Id": "globex"})
    assert r.status_code == 403 and r.json()["error"] == "tenant_mismatch"


async def test_policy_denies_what_the_yaml_says(acme):
    pii = await acme.call_tool("postgres.query", {"sql": "SELECT ssn FROM customers"})
    assert not pii.ok and pii.error_code == "denied_by_policy"
    banned = await acme.call_tool("postgres.query", {"sql": "SELECT 1 FROM orders WHERE x = 'drop'"})
    assert not banned.ok and banned.error_code == "denied_by_policy"
    write = await acme.call_tool("postgres.query", {"sql": "DELETE FROM orders"})
    assert not write.ok and write.error_code == "invalid_arguments"
    hidden = await acme.call_tool("s3.put_object", {"key": "a", "content": "b"})
    assert not hidden.ok and hidden.error_code == "missing_scope"


async def test_rate_limit_stops_a_looping_agent(globex):
    codes = []
    for i in range(30):   # burst is 20, refill 1 per second
        r = await globex.call_tool("elasticsearch.search", {
            "index": "tickets", "query": {"match_all": {}}, "size": i + 1})
        codes.append(r.error_code if not r.ok else "ok")
    assert codes[:15] == ["ok"] * 15
    assert "rate_limited" in codes
    limited = await globex.call_tool("elasticsearch.search", {
        "index": "tickets", "query": {"match_all": {}}, "size": 99})
    assert not limited.ok and limited.retryable is True and "retry after" in limited.hint


async def test_cache_serves_repeats_and_metrics_show_it(acme):
    sql = {"sql": "SELECT id FROM orders WHERE status <> 'e2e-cache-probe' ORDER BY id"}
    first = value(await acme.call_tool("postgres.query", sql))
    second = value(await acme.call_tool("postgres.query", sql))
    assert first == second
    text = compose_exec("atlas-mcp", "curl", "-s", "http://localhost:8080/metrics")
    hits = [line for line in text.splitlines()
            if line.startswith('atlas_cache_hits_total{tool="postgres.query"}')]
    assert hits and float(hits[0].split()[-1]) >= 1


# ── Row-level security, checked in Postgres itself ────────────────────────


def psql(user: str, password: str, sql: str) -> list[str]:
    out = compose_exec("postgres", "psql", "-h", "127.0.0.1", "-U", user, "-d", "atlas",
                       "-tA", "-f", "-", stdin=sql, env={"PGPASSWORD": password})
    return [line for line in out.splitlines() if line and not line.startswith(("BEGIN", "COMMIT"))]


def test_row_level_security_hides_rows_from_the_app_role():
    count = "SELECT count(*) FROM customers;"
    assert psql("atlas_app", "atlas_app", count) == ["0"]                  # no tenant set: nothing
    acme = "BEGIN; SELECT set_config('app.tenant_id','acme',true); " + count + " COMMIT;"
    assert psql("atlas_app", "atlas_app", acme)[-1] == "2"
    globex = "BEGIN; SELECT set_config('app.tenant_id','globex',true); " + count + " COMMIT;"
    assert psql("atlas_app", "atlas_app", globex)[-1] == "1"
    # The compose superuser bypasses RLS entirely, which is why the server must not use it.
    assert psql("atlas", "atlas", count) == ["3"]


def test_database_roles_are_confined_to_their_own_database():
    out = psql("atlas_app", "atlas_app", "INSERT INTO customers VALUES ('x','acme','n','e');")
    assert "permission denied" in " ".join(out) or out == []        # SELECT-only role
    into_keycloak = compose_exec("postgres", "psql", "-h", "127.0.0.1", "-U", "atlas_app",
                                 "-d", "keycloak", "-tAc", "select 1", env={"PGPASSWORD": "atlas_app"})
    assert "permission denied for database" in into_keycloak
    into_atlas = compose_exec("postgres", "psql", "-h", "127.0.0.1", "-U", "keycloak",
                              "-d", "atlas", "-tAc", "select 1", env={"PGPASSWORD": "keycloak"})
    assert "permission denied for database" in into_atlas


# ── Approval flow with S3 ─────────────────────────────────────────────────


async def test_destructive_call_waits_for_a_human_then_runs_once():
    writer_token = await token("writer-acme")
    reviewer_token = await token("reviewer-acme")
    args = {"key": "attachments/e2e.txt", "content": "approved content"}
    async with mcp("writer-acme", writer_token) as writer:
        pending = await writer.call_tool("s3.put_object", args)
        assert not pending.ok and pending.error_code == "pending_approval"
        approval_id = pending.hint.split("approval_id=")[1].split(".")[0]

        with httpx.Client(verify=False, headers={"Authorization": f"Bearer {reviewer_token}",
                                                  "X-Tenant-Id": "acme"}) as reviewer:
            shown = reviewer.get(f"{MCP_URL}/approvals/{approval_id}").json()
            assert shown["tool"] == "s3.put_object" and shown["arguments"]["content"] == args["content"]
            # The agent that asked cannot approve: it has no review scope at all.
            denied = httpx.post(f"{MCP_URL}/approvals/{approval_id}/approve", verify=False,
                                headers={"Authorization": f"Bearer {writer_token}"})
            assert denied.status_code == 403
            done = reviewer.post(f"{MCP_URL}/approvals/{approval_id}/approve")
            assert done.status_code == 200 and done.json()["state"] == "approved"

        ran = await writer.call_tool("s3.put_object", {**args, "approval_id": approval_id})
        assert ran.ok and ran.value["bytes_written"] == len(args["content"])
        replay = await writer.call_tool("s3.put_object", {**args, "approval_id": approval_id})
        assert not replay.ok and replay.error_code == "approval_not_found"

    head = compose_exec("atlas-mcp", "python", "-c",
                        "import boto3,os;c=boto3.client('s3',endpoint_url=os.environ['ATLAS_S3_ENDPOINT']);"
                        "print(c.head_object(Bucket='atlas-mcp-data',Key='acme/attachments/e2e.txt')['ContentLength'])")
    assert head.strip().splitlines()[-1] == str(len(args["content"]))   # stored under the tenant prefix


# ── Observability ─────────────────────────────────────────────────────────


def poll(fn, seconds=60):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(2)
    return last


async def test_audit_log_records_calls_with_trace_ids(acme):
    value(await acme.call_tool("postgres.query", {"sql": "SELECT id FROM orders WHERE id <> 'audit-probe'"}))
    log = compose_exec("atlas-mcp", "cat", "/var/log/atlas/audit.jsonl")
    lines = [json.loads(line) for line in log.splitlines() if line.startswith("{")]
    mine = [x for x in lines if x["tool"] == "postgres.query" and x["status"] == "ok"]
    assert mine and mine[-1]["tenant"] == "acme" and mine[-1]["args_hash"].startswith("sha256:")
    assert mine[-1]["trace_id"] and len(mine[-1]["trace_id"]) == 32
    assert "audit-probe" not in log                      # arguments are hashed, not stored


def test_traces_reach_jaeger():
    def found():
        r = httpx.get("http://localhost:16686/api/traces", params={"service": "atlas-mcp", "limit": 20})
        spans = [s["operationName"] for t in r.json().get("data", []) for s in t["spans"]]
        return [n for n in spans if n.startswith("tool ")]
    assert poll(found), "no `tool ...` spans from atlas-mcp in Jaeger"


def test_prometheus_scrapes_atlas_and_loads_the_alert_rules():
    def up():
        t = httpx.get("http://localhost:9090/api/v1/targets").json()["data"]["activeTargets"]
        return any(x["labels"]["job"] == "atlas-mcp" and x["health"] == "up" for x in t)
    assert poll(up), "atlas-mcp target is not up"
    rules = httpx.get("http://localhost:9090/api/v1/rules").json()["data"]["groups"]
    names = {r["name"] for g in rules for r in g["rules"]}
    assert {"AtlasDown", "AtlasCircuitOpen", "AtlasHighServerErrorRatio"} <= names


def test_grafana_has_the_dashboard_and_both_datasources():
    auth = ("admin", "admin")
    found = poll(lambda: httpx.get("http://localhost:3000/api/search", params={"query": "Atlas"},
                                   auth=auth).json())
    assert any(d["title"] == "Atlas MCP" for d in found)
    kinds = {d["type"] for d in httpx.get("http://localhost:3000/api/datasources", auth=auth).json()}
    assert {"prometheus", "jaeger"} <= kinds


def test_no_service_is_unhealthy():
    import subprocess
    ps = subprocess.run(["docker", "compose", "ps", "--format", "json"],
                        capture_output=True, text=True).stdout
    rows = [json.loads(line) for line in ps.splitlines() if line.strip()]
    bad = [r["Service"] for r in rows if r.get("Health") not in ("", "healthy")]
    assert not bad, bad
    assert len(rows) >= 12, [r["Service"] for r in rows]
