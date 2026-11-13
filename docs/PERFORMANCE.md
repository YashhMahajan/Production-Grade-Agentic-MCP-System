# Performance

Every number here is one of two kinds: measured by a script in this repo, which you can rerun, or published by someone else, with a link. I do not quote a speedup I cannot reproduce. What I have not measured is listed at the bottom.

## Measured: request overhead

`python scripts/bench_pipeline.py 1000` sends tool calls through the whole HTTP path in one process: auth, tenant, MCP session, validation, policy, cache, audit, an OTel span, response. The tool body is faked and instant, so this is the cost of Scout itself, not of Postgres or Qdrant.

Apple M2, Python 3.11.15, `mcp` 1.30.0. Three runs of 1000 calls each, milliseconds:

| Scenario | p50 | p95 |
|----------|-----|-----|
| `GET /healthz` (HTTP stack only) | 0.44 to 0.45 | 0.51 to 0.57 |
| `tools/call`, cache miss | 2.76 to 2.78 | 3.27 to 3.94 |
| `tools/call`, cache hit | 2.72 to 2.75 | 3.17 to 3.39 |

What this says:

- A tool call costs about 2.3 ms more than the bare HTTP stack. I expect most of that is stateless mode, where the MCP session is rebuilt per request so any replica can serve it, but I have not profiled it to confirm. It is the price of horizontal scaling, and I chose it.
- Hit and miss are the same here because the faked tool returns instantly. The cache only pays when the backend is slow. I do not claim a cache speedup until I have run it against real Postgres and Elasticsearch.
- The audit log's stdout write is inside these timings.

## Measured: install size

| | Before | After |
|-|--------|-------|
| Virtualenv (`du -sh mcpvenv`) | 1.2 GB | 463 MB |

I replaced `sentence-transformers` with `fastembed`, which runs the same model through ONNX Runtime and does not install PyTorch.

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

- End-to-end latency against real Postgres, Elasticsearch, Qdrant and Redis.
- What the two cache tiers save against real backends.
- Throughput and tail latency under concurrent load.
- CPU embedding latency with fastembed, and the server's memory use.
- The cost of Postgres row-level security.

None of these can run on my laptop without Docker, and I do not run Docker there. They need a CI job that starts the compose stack and runs a load test. Until that exists, treat any claim about them, mine included, as unproven.
