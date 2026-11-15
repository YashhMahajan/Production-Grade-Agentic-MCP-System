# Memory

Read this first. It is the project's working memory and stands in for CLAUDE.md: what this is, where it stands, the rules, and why things are the way they are. Update it when any of that changes.

## What this is

Scout MCP: a multi-tenant MCP server that gives agents access to Postgres, Elasticsearch, Qdrant and S3, plus a four-agent support copilot (Planner, Retriever, Synthesizer, Critic). The owner wants it to be something engineers learn from and companies deploy, and to be backed by numbers. Repo: github.com/YashhMahajan/Production-Grade-Agentic-MCP-System. It was renamed from Atlas MCP on 2026-11-13: package `scout_mcp`, env prefix `SCOUT_`, Keycloak realm `scout`, audience `scout-mcp`.

Docs in `docs/`, exactly six files: ARCHITECTURE, ISSUES, PRD, AGENTS, PERFORMANCE, DATA-MODEL. New knowledge goes into one of them or into this file. Do not add other markdown files.

## State (0.5.0)

0.5.0 is the ten server features, in `docs/ISSUES.md` 64 to 75, each row with the test that guards it. 293 in-process tests and 41 end-to-end tests pass in CI against the real 12-service stack, and `ruff` is clean. Every new guarantee was checked by breaking the code and confirming a test failed.

- CI has four jobs: `test`, `validate-deploy`, `stack`, `security`. Start one on a branch with `gh workflow run ci.yml --ref <branch>`; it also runs on pull requests.
- The environment I built this in had a metered connection, so the loop was edit, push the branch, read CI. That works and costs the laptop nothing. Download size matters to the owner; ask before installing.

Lessons that are not in the code:
- Keycloak 26 puts `sub` in a token only if a mapper adds it. This realm defines only its own client scopes, so the built-in `basic` scope does not exist and `scout-human` has an explicit `oidc-sub-mapper`. Without it the server refuses the token. `tests/test_realm.py` guards it.
- The login form's cookies must be sent explicitly in the e2e `sign_in()`: Python's cookie jar withholds them from plain `http://localhost`, and Keycloak answers 400 "Cookie not found".
- The middleware logs why a token was rejected (code and reason, never the token). That line found the `sub` bug in one CI run.
- Two guard layers that cover the same hole hide each other from a mutation check. Test each layer on its own.
- Elasticsearch document ids are global, so two tenants can never share one; ingest ids are `doc_<tenant>_<slug>` and a clash is an error.

## Rules (the owner's, stated in session)

- Never start Docker or anything heavy on the owner's Mac. Push a branch and read the CI run instead. `gh workflow run ci.yml --ref <branch>` starts one (only `main` and `ship-ready` trigger on push).
- `.env` is gitignored and may be edited when asked, but never print or commit its values. Mirror new variables in `.env.example`.
- Stage files by name (`git add <paths>`), never `git add -A` or `git add -A tests`: it once committed a test file whose implementation did not exist yet, and CI went red for a reason that had nothing to do with the change.
- The local venv has `mcp` 2.3.0 (upgraded with the owner's OK; it had 1.30.0, so every server test errored). The pip download cache was purged at the owner's request (416.8 MB), so a reinstall downloads again.
- The session scratchpad can disappear mid-session. Keep backups of files for mutation checks in memory inside one Python process, not as files.
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
- **Review needs `approval:review` and `role:reviewer`.** The scope can be requested by any client that offers it; the role is the identity provider's record of the person.
- **A tool's result is checked against its `output_schema` before it is cached.** A wrong schema breaks every compliant client, so the server names the tool instead.
- **`postgres.describe` is never cached and reads the caller from `tools.base.CALLER`.** The cache key has no caller in it.
- **Ingest writes Qdrant, then Postgres, then Elasticsearch with the content hash last.** A crash leaves a document that is done again, never one that looks done.
- **Commands print config changes; they do not make them.** `tenant add`, `new-tool` and `policy` leave `config/` to a person.
- **`mcp>=2.3,<3`.** 2.x replaced the decorators with `on_list_tools` and `on_call_tool` on `Server(...)`; the verified principal comes from `ctx.request`. The models still accept camelCase constructor arguments.
- **A guard that is blunt on purpose.** `postgres.query` refuses `set_config`, `pg_*` and any quote, comment or dollar quoting, even inside a string. A cleverer filter is a bypass waiting to happen; the benchmark had to change instead.
- **Approvals are `SET NX` keys**, not a record that is rewritten: a decision and a claim each have one winner.
- **The agent client waits out a rate limit once.** Tests that want the server's raw answer use `_call_once`.
- **`elasticsearch<9`.** The compose server is 8.14.
- **Server connects to Postgres as `atlas_app`.** The compose superuser bypasses row-level security. The role name predates the rename.
- **Elasticsearch exact-match fields are `keyword`.** The default mapping breaks `term` queries and lets tenant `acme` match `acme-corp`.
- **Tool errors return as `isError` results** with `{code, retryable, hint}`, because the SDK flattens raised exceptions to text. Results are JSON-normalised in the pipeline because backends return datetimes and UUIDs.
- **Stores on an internal Docker network, each with a credential.** Only Caddy (8443) and four UIs reach the host, on 127.0.0.1.
- **Keycloak, client-credentials grant, issuer pinned to `keycloak:8080`** so a token has the same `iss` from the host and from inside the network.
- **SeaweedFS for S3.** MinIO stopped publishing images (gone from Docker Hub and Quay).
- **Destructive tools wait for a different identity with `approval:review`.** The requester cannot approve its own call.
- **LLM is a two-model waterfall on one OpenRouter key** (merged 2026-11-15, PR #1): `SCOUT_LLM_MODEL=google/gemma-4-31b-it:free`, then `SCOUT_LLM_FALLBACK_MODEL=inclusionai/ling-3.2-flash:free`. Empty fallback turns it off. No fallback on 400/401/402/403. The free tier is about 50 requests a day without credit, so never make live LLM calls in tests or CI. The key is only in the gitignored `.env`; the owner asked that it be handled carefully. Free models send customer text to third-party hosts, so which providers may see it is the owner's decision.
- **`SCOUT_ENV=production` refuses development settings** (`ServerSettings.production_problems`). `AGENTS.md` at the root links to `docs/AGENTS.md`; `llms.txt` indexes the docs. `tests/test_hardening.py` checks every tool's scopes, approval and rate-limit names.
- **The runtime image has no pip, setuptools or wheel.** They carried advisories and nothing builds at runtime. It also gets Debian's security updates at build time.
- **Images are pinned and checked for existence first.** The Trivy setup action pinned a release that had been removed, so Trivy runs from its own pinned image.
- **Ruff pinned to `E4,E7,E9,F`** so a newer ruff does not add rules.
- **Caddy keeps `NET_BIND_SERVICE`** because it cannot exec with every capability dropped.

## Pointers

- Request path: `src/scout_mcp/server.py` `_run_pipeline`
- Policy rules: `config/policy.yaml`, engine in `auth/policy.py`
- Tool base and `authorize`: `tools/base.py`
- In-process test harness: `tests/conftest.py` (`app_client` runs the real server with Redis, embeddings and token validation faked)
- End-to-end tests: `tests/e2e/`, run only with `SCOUT_E2E=1` against a running stack
- Realm: `deploy/keycloak/scout-realm.json`; seed data: `deploy/sql/init.sql`, `deploy/seed/seed.py`
