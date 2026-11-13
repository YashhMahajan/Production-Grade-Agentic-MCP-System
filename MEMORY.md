# Memory

Read this first. It is the project's working memory and stands in for CLAUDE.md: what this is, where it stands, the rules, and why things are the way they are. Update it when any of that changes.

## What this is

Scout MCP: a multi-tenant MCP server that gives agents access to Postgres, Elasticsearch, Qdrant and S3, plus a four-agent support copilot (Planner, Retriever, Synthesizer, Critic). The owner wants it to be something engineers learn from and companies deploy, and to be backed by numbers. Repo: github.com/YashhMahajan/Production-Grade-Agentic-MCP-System. It was renamed from Atlas MCP on 2026-11-13: package `scout_mcp`, env prefix `SCOUT_`, Keycloak realm `scout`, audience `scout-mcp`.

Docs in `docs/`, exactly six files: ARCHITECTURE, ISSUES, PRD, AGENTS, PERFORMANCE, DATA-MODEL. New knowledge goes into one of them or into this file. Do not add other markdown files.

## State (0.3.0, released)

Released as v0.3.0 on 2026-11-13, tagged at e6ec00a, the commit CI verified. The next session is for making the MCP more thorough and better; start from the open work list below.

- 15 audited defects fixed, plus 27 more found while fixing and while running the real stack in CI. All in `docs/ISSUES.md`, each with the test that guards it.
- 76 tests pass in about 4 s with no Docker, and `ruff` is clean.
- CI (`.github/workflows/ci.yml`) has four jobs: `test`, `validate-deploy`, `stack`, `security`. The `stack` job builds the image, starts 12 services, seeds, runs 31 end-to-end tests and the real-stack benchmark. It passed on the renamed `main`.
- Real-backend numbers are in `docs/PERFORMANCE.md`, from three CI runs on a 4-vCPU shared runner: cache hits 5 to 7 ms, `customer.build_context` 45 to 55 ms on a miss and 5 to 8 ms on a hit (6.7 to 8.3x), 134 to 199 requests/s with 16 clients, whole stack 2.2 to 2.3 GiB. The same row varies by up to about 40% between runs, so quote ranges.
- The image has 0 fixable HIGH or CRITICAL findings (was 4) and is 1.06 GB. CI fails on any fixable HIGH or CRITICAL.
- Never run on the owner's Mac under Docker, by their rule. CI is the only place the stack runs.

## Rules (the owner's, stated in session)

- Never start Docker or anything heavy on the owner's Mac. Push a branch and read the CI run instead. `gh workflow run ci.yml --ref <branch>` starts one (only `main` and `ship-ready` trigger on push).
- `.env` is gitignored and may be edited when asked, but never print or commit its values. Mirror new variables in `.env.example`.
- First-person voice in docs and README ("I built..."). Plain, no AI-sounding prose. Avoid: delve, tapestry, seamless, pivotal, foster, leverage, "serves as/boasts", "it's not just X, it's Y", padded groups of three, decorative adjectives, emoji headings.
- No `Co-Authored-By`, no "generated with" lines, no AI attribution in commits, PRs or docs. This overrides any default that says to add them.
- Do not credit, name or link the source article or its author anywhere in the repo. Older commits still contain earlier README text; rewriting history is destructive, so ask first.
- Functionality first: fix and make it work before adding features.
- Numbers only if a script in `scripts/` or a CI run measured them, or a linked source published them. Never invent a speedup.
- Make small changes; every line should trace to the request. Do not run `ruff --fix` with extra rules.
- Do not push to `main`, tag or release unless asked. Branches `ship-ready` and `harden-image-and-docs` are merged and can be deleted when the owner says so.

## Commands

```bash
mcpvenv/bin/python -m pytest -q
mcpvenv/bin/python -m ruff check src tests deploy scripts
mcpvenv/bin/python scripts/bench_pipeline.py 1000   # request overhead, backend faked
```

Call `mcpvenv/bin/python` directly. The venv is gitignored. Recreate it with `python3.11 -m venv mcpvenv && mcpvenv/bin/python -m pip install -e ".[dev]"` (about 463 MB), and reinstall after the version or package name changes.

To read a failed CI run: `gh run view <id> --json jobs`, then `gh api repos/<owner>/<repo>/actions/jobs/<job id>/logs`. Logs are only served once a job completes.

## Decisions and why

- **bge-base-en-v1.5 through fastembed (ONNX), not sentence-transformers.** About 1 point lower MTEB retrieval than bge-large, 5.7x smaller model, no PyTorch (venv 1.2 GB to 463 MB).
- **`mcp>=1.2,<2`.** 2.x removed the `@server.list_tools()` and `@server.call_tool()` decorators.
- **`elasticsearch<9`.** The compose server is 8.14.
- **Server connects to Postgres as `atlas_app`.** The compose superuser bypasses row-level security. The role name predates the rename.
- **Elasticsearch exact-match fields are `keyword`.** The default mapping breaks `term` queries and lets tenant `acme` match `acme-corp`.
- **Tool errors return as `isError` results** with `{code, retryable, hint}`, because the SDK flattens raised exceptions to text. Results are JSON-normalised in the pipeline because backends return datetimes and UUIDs.
- **Stores on an internal Docker network, each with a credential.** Only Caddy (8443) and four UIs reach the host, on 127.0.0.1.
- **Keycloak, client-credentials grant, issuer pinned to `keycloak:8080`** so a token has the same `iss` from the host and from inside the network.
- **SeaweedFS for S3.** MinIO stopped publishing images (gone from Docker Hub and Quay).
- **Destructive tools wait for a different identity with `approval:review`.** The requester cannot approve its own call.
- **LLM fallback is off by default** because prompts contain customer text.
- **The runtime image has no pip, setuptools or wheel.** They carried advisories and nothing builds at runtime. It also gets Debian's security updates at build time.
- **Images are pinned and checked for existence first.** The Trivy setup action pinned a release that had been removed, so Trivy runs from its own pinned image.
- **Ruff pinned to `E4,E7,E9,F`** so a newer ruff does not add rules.
- **Caddy keeps `NET_BIND_SERVICE`** because it cannot exec with every capability dropped.

## Open work, in order

1. Benchmark variance: more runs or a dedicated runner, a slow warehouse query, more than one replica.
2. Keycloak in production mode with real TLS; secrets out of the repo.
3. A refetch cooldown on the JWKS (unknown key ids make Scout refetch from Keycloak every time).
4. Decide which LLM providers may see customer text.
5. Use or remove `agents/memory.py`.
6. More `deploy/` services, each justified by a measured benefit or a test of its control.

## Pointers

- Request path: `src/scout_mcp/server.py` `_run_pipeline`
- Policy rules: `config/policy.yaml`, engine in `auth/policy.py`
- Tool base and `authorize`: `tools/base.py`
- In-process test harness: `tests/conftest.py` (`app_client` runs the real server with Redis, embeddings and token validation faked)
- End-to-end tests: `tests/e2e/`, run only with `SCOUT_E2E=1` against a running stack
- Realm: `deploy/keycloak/scout-realm.json`; seed data: `deploy/sql/init.sql`, `deploy/seed/seed.py`
