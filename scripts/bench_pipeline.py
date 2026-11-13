"""Measure the server's own overhead per tool call, with the backend faked.

What it times: HTTP request -> auth middleware -> tenant middleware -> MCP session
-> validation -> scope and policy check -> cache -> (fake) tool -> audit -> response.
The tool body is instant, so the numbers are the cost of the request path, not of
Postgres or Qdrant. It runs in one process with no network and no containers.

    python scripts/bench_pipeline.py            # 500 calls per scenario
    python scripts/bench_pipeline.py 2000
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import httpx
from opentelemetry.sdk.trace import TracerProvider

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas_mcp.auth import middleware  # noqa: E402
from atlas_mcp.cache.manager import CacheManager  # noqa: E402
from atlas_mcp.config import ServerSettings  # noqa: E402
from atlas_mcp.ratelimit.limiter import RateLimiter  # noqa: E402
from atlas_mcp.server import AtlasServer, build_http_app  # noqa: E402
from atlas_mcp.tools.atomic.postgres import PostgresQueryTool  # noqa: E402
from tests.conftest import HEADERS, FakeRedis, _Validator  # noqa: E402


# A real tracer with no exporter: spans are created and recorded, none leave the process.
tracer = TracerProvider().get_tracer("bench")


async def noop(self, *a, **k):
    return None


async def fake_run(self, tenant, args):
    return {"columns": ["id"], "rows": [{"id": 1}], "row_count": 1, "truncated": False}


def body(sql: str) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "postgres.query", "arguments": {"sql": sql}}}


async def timed(client, payload, headers) -> float:
    start = time.perf_counter()
    resp = await client.post("/mcp", json=payload, headers=headers)
    elapsed = (time.perf_counter() - start) * 1000
    assert resp.status_code == 200 and '"isError":true' not in resp.text, resp.text[:200]
    return elapsed


def summarize(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {"p50": statistics.median(ordered),
            "p95": ordered[int(len(ordered) * 0.95) - 1],
            "mean": statistics.fmean(ordered)}


async def main(n: int) -> str:
    headers = {**HEADERS, "Authorization": "Bearer support"}
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(CacheManager, "connect", noop), \
            patch.object(RateLimiter, "connect", noop), \
            patch.object(RateLimiter, "disconnect", noop), \
            patch.object(RateLimiter, "acquire", noop), \
            patch.object(PostgresQueryTool, "run", fake_run), \
            patch.object(middleware, "get_validator", lambda s: _Validator()), \
            patch("atlas_mcp.tools.atomic.embeddings.get_embedder", lambda: object()), \
            patch("atlas_mcp.server.init_tracing", lambda s: None), \
            patch("atlas_mcp.server.get_tracer", lambda: tracer):
        settings = ServerSettings(
            _env_file=None, policy_file=str(ROOT / "config/policy.yaml"),
            audit_log_path=f"{tmp}/audit.jsonl")
        server = AtlasServer(settings)
        app = build_http_app(server)
        started, stop = asyncio.Event(), asyncio.Event()

        async def lifespan():
            async with app.router.lifespan_context(app):
                server._redis = FakeRedis()
                started.set()
                await stop.wait()

        task = asyncio.create_task(lifespan())
        await started.wait()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
            for i in range(50):  # warm up imports, JIT-ish caches, the connection pool
                await timed(client, body(f"SELECT id FROM warm{i}"), headers)
            miss = [await timed(client, body(f"SELECT id FROM t{i}"), headers) for i in range(n)]
            await timed(client, body("SELECT id FROM hot"), headers)
            hit = [await timed(client, body("SELECT id FROM hot"), headers) for _ in range(n)]
            ping = []
            for _ in range(n):  # floor: the HTTP stack and auth with no tool call at all
                start = time.perf_counter()
                await client.get("/healthz")
                ping.append((time.perf_counter() - start) * 1000)
        stop.set()
        await task

    return json.dumps({
        "python": platform.python_version(),
        "machine": f"{platform.machine()} / {platform.system()} {platform.release()}",
        "calls_per_scenario": n,
        "unit": "milliseconds per call",
        "healthz_no_auth": summarize(ping),
        "tool_call_cache_miss": summarize(miss),
        "tool_call_cache_hit": summarize(hit),
    }, indent=2)


if __name__ == "__main__":
    # The audit logger mirrors every call to stdout. That write stays in the timing,
    # but goes to /dev/null so the report below is the only output.
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
        report = asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 500))
    print(report)
