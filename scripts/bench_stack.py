"""Benchmark the running compose stack through the TLS edge, with real backends.

Unlike bench_pipeline.py this includes Postgres, Elasticsearch, Qdrant, Redis,
the embedding model, TLS and the reverse proxy. It needs the stack up and seeded,
and per-call rate limits raised so the limiter does not shape the result:

    SCOUT_RATE_LIMIT_DEFAULT_RPM=600000 SCOUT_RATE_LIMIT_BURST=100000 \\
    SCOUT_RATE_LIMIT_PER_TOOL_OVERRIDES=false docker compose up -d scout-mcp
    python scripts/bench_stack.py [calls_per_scenario] [concurrent_seconds]

Prints one JSON document. A "cache speedup" is the miss p50 divided by the hit p50,
so it is measured on this machine, not assumed.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import sys
import time

import httpx

from scout_mcp.agents.mcp_client import _decode_body, fetch_token

MCP_URL = os.environ.get("SCOUT_E2E_MCP_URL", "https://localhost:8443")
TOKEN_URL = os.environ.get(
    "SCOUT_E2E_TOKEN_URL", "http://localhost:8081/realms/scout/protocol/openid-connect/token")
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def pct(samples: list[float], p: float) -> float:
    ordered = sorted(samples)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * p) - 1))]


def summarize(samples: list[float]) -> dict:
    return {"n": len(samples), "p50_ms": round(statistics.median(samples), 2),
            "p95_ms": round(pct(samples, 0.95), 2), "p99_ms": round(pct(samples, 0.99), 2),
            "mean_ms": round(statistics.fmean(samples), 2)}


async def call(client: httpx.AsyncClient, headers: dict, tool: str, arguments: dict) -> float:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    start = time.perf_counter()
    resp = await client.post(f"{MCP_URL}/mcp", json=body, headers=headers)
    elapsed = (time.perf_counter() - start) * 1000
    result = _decode_body(resp)["result"]
    if result.get("isError"):
        raise RuntimeError(f"{tool}: {result['content'][0]['text']}")
    return elapsed


async def series(client, headers, tool, make_args, n) -> list[float]:
    return [await call(client, headers, tool, make_args(i)) for i in range(n)]


async def throughput(client_factory, headers, tool, make_args, seconds: float, workers: int) -> dict:
    done, errors, latencies = 0, 0, []
    stop = time.monotonic() + seconds

    async def worker(w: int):
        nonlocal done, errors
        async with client_factory() as client:
            i = 0
            while time.monotonic() < stop:
                try:
                    latencies.append(await call(client, headers, tool, make_args(w, i)))
                    done += 1
                except Exception:
                    errors += 1
                i += 1

    began = time.monotonic()
    await asyncio.gather(*(worker(w) for w in range(workers)))
    wall = time.monotonic() - began
    return {"workers": workers, "seconds": round(wall, 1), "requests": done, "errors": errors,
            "requests_per_second": round(done / wall, 1), **summarize(latencies or [0.0])}


async def main(n: int, seconds: float) -> dict:
    bearer = await fetch_token(TOKEN_URL, "copilot-acme", "dev-secret-copilot-acme")
    headers = {**HEADERS, "Authorization": f"Bearer {bearer}", "X-Tenant-Id": "acme"}

    def factory():
        return httpx.AsyncClient(verify=False, timeout=60)

    token_times = []
    for _ in range(20):
        start = time.perf_counter()
        await fetch_token(TOKEN_URL, "copilot-acme", "dev-secret-copilot-acme")
        token_times.append((time.perf_counter() - start) * 1000)

    pg = lambda i: {"sql": f"SELECT id, status FROM orders WHERE status <> 'miss-{i}' ORDER BY id"}  # noqa: E731
    ctx = lambda i: {"customer_id": "CUST-1001", "question": f"refund question number {i}"}  # noqa: E731
    sem = lambda i: {"query": f"how long do refunds take {i}", "collection": "support_docs",  # noqa: E731
                     "hydrate_from_postgres": False}

    results: dict = {}
    async with factory() as client:
        for i in range(30):                      # warm connections, pools, the model
            await call(client, headers, "postgres.query", pg(10_000 + i))
            await call(client, headers, "customer.build_context", ctx(10_000 + i))
        for name, tool, make in (("postgres.query", "postgres.query", pg),
                                 ("customer.build_context", "customer.build_context", ctx),
                                 ("semantic_search", "semantic_search", sem)):
            miss = await series(client, headers, tool, make, n)
            await call(client, headers, tool, make(-1))
            hit = await series(client, headers, tool, lambda _i, m=make: m(-1), n)
            results[name] = {"cache_miss": summarize(miss), "cache_hit": summarize(hit),
                             "cache_speedup_p50": round(statistics.median(miss) / statistics.median(hit), 1)}
    results["concurrent_cache_hits"] = await throughput(
        factory, headers, "postgres.query", lambda w, i: pg(-1), seconds, 16)
    results["concurrent_cache_misses"] = await throughput(
        factory, headers, "postgres.query", lambda w, i: pg(f"{w}-{i}"), seconds, 16)

    return {"machine": f"{platform.machine()} / {platform.system()} {platform.release()}",
            "cpus": os.cpu_count(), "calls_per_scenario": n, "unit": "ms per call, client side, via TLS edge",
            "token_fetch_p50_ms": round(statistics.median(token_times), 1), **results}


if __name__ == "__main__":
    calls = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    print(json.dumps(asyncio.run(main(calls, secs)), indent=2))
