# Performance

Every number here is one of two kinds: measured by a script in this repo, which you can rerun (the real-stack ones come from a CI run you can open), or published by someone else, with a link. I do not quote a speedup I cannot reproduce. What I have not measured is listed at the bottom.

## Measured: the whole stack, real backends

`scripts/bench_stack.py` runs in CI against the full compose stack, through the TLS edge: Caddy, JWT verification, the MCP session, the policy check, then real Postgres, Elasticsearch, Qdrant, Redis and the embedding model. Times are client-side milliseconds.

Hardware: a GitHub-hosted runner, x86_64, 4 vCPUs, Linux. All 12 containers and the load generator share it, and Scout runs as one uvicorn worker. Three CI runs, 200 calls per row, on the same code: [43418179935](https://github.com/YashhMahajan/Production-Grade-Agentic-MCP-System/actions/runs/43418179935), [43419095049](https://github.com/YashhMahajan/Production-Grade-Agentic-MCP-System/actions/runs/43419095049) and [43421061388](https://github.com/YashhMahajan/Production-Grade-Agentic-MCP-System/actions/runs/43421061388). Each cell is the lowest and highest of the three.

| Tool | Cache miss p50 | p95 | p99 | Cache hit p50 | p95 | p99 | Speedup at p50 |
|------|---------------|-----|-----|---------------|-----|-----|----------------|
| `postgres.query` | 6.0 to 8.9 | 7.1 to 13.2 | 10.8 to 18.0 | 4.9 to 7.2 | 5.2 to 8.2 | 5.9 to 9.3 | 1.2 to 1.4x |
| `customer.build_context` | 45.2 to 55.4 | 65.7 to 78.9 | 95.7 to 118.1 | 5.4 to 8.3 | 6.8 to 11.2 | 8.0 to 20.8 | 6.7 to 8.3x |
| `semantic_search` | 36.4 to 44.8 | 58.7 to 78.9 | 82.5 to 101.8 | 5.0 to 7.2 | 6.0 to 8.2 | 9.8 to 12.4 | 6.2 to 7.2x |

What this says:

- The cache pays in proportion to the work it skips. `customer.build_context` fans out to two Postgres queries, Elasticsearch, and an embedding plus a Qdrant search: 45 to 55 ms on a miss, 5 to 8 ms on a hit. A plain `SELECT` against a four-row table only goes from 6 to 9 ms down to 5 to 7 ms, because Postgres was already fast. I have not measured a slow warehouse query, which is where I would expect the biggest win.
- A hit still costs 5 to 7 ms. That floor is TLS, Caddy, RS256 token verification and the MCP session, not the backends.
- A `semantic_search` miss includes embedding the query on CPU with bge-base through ONNX. I have not separated that time out.
- The runner is shared hardware, and it shows. The same row moves by as much as 40% between runs, and tail latencies move more (`customer.build_context` hit p99 was 8.0 ms in two runs and 20.8 ms in the third). Trust the ordering and the speedups. Do not trust the second digit.

Sixteen concurrent clients for 10 seconds on the same runner:

| Workload | Requests/s | p50 ms | p95 ms | p99 ms | Requests | Errors |
|----------|-----------|--------|--------|--------|----------|--------|
| `postgres.query`, cache hits | 134 to 199 | 73 to 110 | 142 to 199 | 169 to 207 | 1,344 to 1,994 | 0 |
| `postgres.query`, cache misses | 114 to 161 | 86 to 129 | 216 to 257 | 239 to 299 | 1,148 to 1,616 | 0 |

The runner's CPUs were the limit: one Python worker, twelve other containers, and the client all on 4 vCPUs. Running more Scout replicas behind the proxy is the obvious lever, and I have not tried it. No request failed in any run.

Token fetch from Keycloak (client-credentials grant), p50: 19.8 to 29.0 ms.

## Measured: memory

`docker stats` after the benchmark, same three runs. Every container has a limit in `docker-compose.yml`.

| Container | Used (MiB) | Limit |
|-----------|-----------|-------|
| elasticsearch | 963 to 966 | 1280 MiB |
| scout-mcp | 544 to 545 | 1536 MiB |
| keycloak | 461 to 506 | 1 GiB |
| postgres | 81 to 82 | 512 MiB |
| grafana | 50 to 52 | 256 MiB |
| otel-collector | 35 to 36 | 256 MiB |
| qdrant | 31 to 33 | 512 MiB |
| prometheus | 25 to 28 | 512 MiB |
| caddy | 21 to 22 | 128 MiB |
| jaeger | 20 to 23 | 256 MiB |
| s3 (SeaweedFS) | 19 | 512 MiB |
| redis | 4.5 to 5 | 320 MiB |
| **Total** | **2,262 to 2,310 (about 2.2 GiB)** | |

Elasticsearch is the largest by a wide margin, and Scout itself is second because the embedding model is loaded once into its process.

## Measured: request overhead, backend faked

`python scripts/bench_pipeline.py 1000` runs the request path in one process with a faked, instant tool. It isolates what Scout itself costs.

Apple M2, Python 3.11.15, `mcp` 1.30.0. Three runs of 1000 calls, milliseconds:

| Scenario | p50 | p95 |
|----------|-----|-----|
| `GET /healthz` (HTTP stack only) | 0.44 to 0.45 | 0.51 to 0.57 |
| `tools/call`, cache miss | 2.76 to 2.78 | 3.27 to 3.94 |
| `tools/call`, cache hit | 2.72 to 2.75 | 3.17 to 3.39 |

A tool call costs about 2.3 ms more than the bare HTTP stack. I expect most of that is stateless mode, where the MCP session is rebuilt per request so any replica can serve it, but I have not profiled it. The audit log's stdout write is inside these timings. Hit and miss are equal here because the faked tool returns instantly.

## Measured: install size and dependencies

- Virtualenv (`du -sh mcpvenv`): 1.2 GB before, 463 MB after. I replaced `sentence-transformers` with `fastembed`, which runs the same model through ONNX Runtime and does not install PyTorch.
- `pip-audit` on the dependencies: no known vulnerabilities (CI, every run).
- Image size: `scout-mcp:dev` is 1.06 GB. I have not broken that down. The embedding model is 0.21 GB of it.
- Image vulnerabilities, Trivy on the built image, fixable HIGH and CRITICAL findings: 4 before (2 in Debian's `zlib1g`, 2 in `wheel` and `jaraco.context` bundled with the base image's packaging tools), 0 after. The fix was Debian's security updates plus removing pip, setuptools and wheel from the runtime image, which nothing there needs. CI fails the build on any fixable HIGH or CRITICAL finding, so the count stays at 0.

## Published: the embedding model choice

I use `BAAI/bge-base-en-v1.5` rather than `bge-large-en-v1.5`.

| | bge-base | bge-large | Source |
|-|----------|-----------|--------|
| Parameters | 109M | 335M | [BGE leaderboard](https://bge.baai.ac.cn/) |
| Dimensions | 768 | 1024 | same |
| MTEB retrieval average (15 datasets) | 53.25 | 54.29 | same |
| MTEB overall average (56 datasets) | 63.56 | 64.23 | same |
| ONNX model size | 0.21 GB | 1.2 GB | fastembed's model registry |
| Query latency, A100 GPU, approximate | ~10 ms | ~25 ms | [General Compute](https://www.generalcompute.com/blog/embedding-model-benchmarks-speed-vs-quality-for-rag-retrieval) |

Arithmetic from those figures: retrieval is 1.04 points lower (about 2%). The model is 3.1x smaller in parameters and 5.7x smaller on disk. Each float32 vector takes 3,072 bytes instead of 4,096, so a million vectors need about 3.07 GB instead of 4.10 GB before index overhead. The latency row is a third party's approximate GPU figure, not my measurement on CPU.

## Not measured yet

- Variance beyond three runs. Three samples show the spread (up to about 40% on a row) but not its distribution.
- Load beyond 16 clients, and more than one Scout replica.
- A slow, realistic warehouse query, to see what the cache saves there.
- The embedding step on its own, and the cost of Postgres row-level security on a large table.
