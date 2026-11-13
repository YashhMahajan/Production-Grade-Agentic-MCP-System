# Memory

Read this first. It is the project's working memory and stands in for CLAUDE.md: what this is, where it stands, the rules, and why things are the way they are. Update it when any of that changes.

## What this is

Atlas MCP: a multi-tenant MCP server that gives agents access to Postgres, Elasticsearch, Qdrant and S3, plus a four-agent support copilot (Planner, Retriever, Synthesizer, Critic). The owner built it from the structure of Fareed Khan's article "Building the 11 Layers of a Production-Grade MCP Server + Agentic System" and wants it to be something engineers learn from and companies deploy. Repo: github.com/YashhMahajan/Production-Grade-Agentic-MCP-System.

Docs in `docs/`: ARCHITECTURE, ISSUES, PRD, AGENTS, PERFORMANCE, DATA-MODEL. Also older DEPLOYMENT.md and AGENT_SYSTEM.md (not reviewed since the 0.2.0 changes). `docs/STARTUP.md` is local and gitignored.

## State (0.2.0)

- 15 audited defects fixed, plus nine more found on the way. Full list in `docs/ISSUES.md`.
- 51 tests pass without Docker in about 3 s. `ruff` is clean.
- The whole request path is tested in-process: HTTP, auth, tenant, policy.yaml, tools, approval, audit, spans, metrics.
- Never run against real Postgres, Elasticsearch, Qdrant, Redis or MinIO. Compose, Dockerfile, `init.sql` and `seed.py` are written and parsed only.
- **Blocker for a working demo:** no token issuer. The server verifies JWTs against a JWKS URL and nothing in compose provides one.

## Rules (the owner's, stated in session)

- Never start Docker or anything heavy on the owner's Mac. Integration runs go in GitHub Actions.
- Do not touch `.env` (real secrets). Edit `.env.example` and say which `.env` lines to change. Current `.env` still needs: `ATLAS_EMBEDDING_MODEL=BAAI/bge-base-en-v1.5`, `ATLAS_POSTGRES_DSN` using `atlas_app`, vector dimension 768.
- First-person voice in docs and README ("I built..."). Plain, no AI-sounding prose. Avoid: delve, tapestry, seamless, pivotal, foster, leverage, "serves as/boasts", "it's not just X, it's Y", padded groups of three, decorative adjectives, emoji headings.
- No `Co-Authored-By`, no "generated with" lines, no AI attribution in commits, PRs or docs.
- Functionality first: fix and make it work before adding features.
- Numbers only if a script in `scripts/` measured them or a linked source published them. Never invent a speedup.
- `docs/` holds only the six files above plus the two older ones. Do not add more.
- Make small changes; every line should trace to the request. Do not run `ruff --fix` with extra rules.
- Do not push, tag or release unless asked. (A 0.2.0 release was requested and made.)

## Commands

```bash
mcpvenv/bin/python -m pytest -q
mcpvenv/bin/python -m ruff check src tests deploy scripts
mcpvenv/bin/python scripts/bench_pipeline.py 1000
```

Call `mcpvenv/bin/python` directly. The venv is gitignored. Recreate it with `python3.11 -m venv mcpvenv && mcpvenv/bin/python -m pip install -e ".[dev]"` (about 463 MB).

## Decisions and why

- **bge-base-en-v1.5 via fastembed (ONNX), not sentence-transformers.** About 1 point lower MTEB retrieval than bge-large, 5.7x smaller model, and no PyTorch (venv 1.2 GB to 463 MB).
- **`mcp>=1.2,<2`.** 2.x removed the `@server.list_tools()` / `@server.call_tool()` decorators.
- **`elasticsearch<9`.** The compose server is 8.14.
- **Server connects to Postgres as `atlas_app`.** The compose superuser bypasses row-level security.
- **Tool errors return as `isError` results** with `{code, retryable, hint}` JSON, because the SDK flattens raised exceptions to text.
- **Destructive tools stay pending until a human approves that exact call.** No approval endpoint exists yet, so they stay pending. That is deliberate and safe.
- **Stateless MCP sessions.** Costs about 2.3 ms per call over the bare HTTP stack; lets any replica serve any request.
- **Ruff pinned to `E4,E7,E9,F`** so a newer ruff does not add rules.

## Open work, in order

1. Token issuer in compose (Keycloak or similar) so the copilot can run end to end.
2. GitHub Actions: compose up, seed, end-to-end test, load test. Gives real-backend numbers for PERFORMANCE.md.
3. Approval review endpoint.
4. More services in `deploy/` for security, performance, operations. Justify each with a control or a number before adding.
5. Wire or remove: ATBA, agent memory, the two unused gauges.
6. Decide on the LLM provider fallback chain (it sends customer text to several providers).
7. README: it hotlinks the original article's images and points at `images/`, which is gitignored. Replace with the owner's own diagrams, and rewrite in first person.

## Pointers

- Request path: `src/atlas_mcp/server.py` `_run_pipeline`
- Policy rules: `config/policy.yaml`, engine in `auth/policy.py`
- Tool base and `authorize`: `tools/base.py`
- Test harness: `tests/conftest.py` (`app_client` runs the real server with Redis, embeddings and token validation faked)
- Seed data: `deploy/sql/init.sql`, `deploy/seed/seed.py`
