# Agent rules

Rules for any AI assistant or script working in this repo, then the limits the copilot itself runs under. If a rule here conflicts with a request, stop and ask.

## Hard guardrails

1. **Do not start Docker, Compose, or any container, VM, or large download on the developer's machine.** Check compose files and Dockerfiles statically (YAML parses, `ruff`, `docker compose config` in CI). Anything that needs a running container runs in GitHub Actions: push a branch and read the run.
2. **Never print or commit values from `.env`.** It is gitignored and holds real keys. You may edit it when the owner asks. Mirror every new variable in `.env.example` with a safe development value.
3. **Do not weaken isolation or deny-by-default.** Never remove a tenant filter, widen `scout_app`'s grants, let the server connect as a superuser, set `SCOUT_POLICY_DEFAULT_DENY=false`, or add a policy rule without a test that shows who it allows.
4. **Keep the data stores private and authenticated.** Postgres, Redis, Elasticsearch, Qdrant and the S3 store stay on the internal `data` network with no published ports, and each keeps its password or key. Do not publish a port on anything except `127.0.0.1`.
5. **Give each service only the secrets it uses.** `scout-mcp` lists its settings under `environment:`; do not give it `env_file: .env`, which would put the LLM keys in the server container.
6. **A new tool needs four things:** a Pydantic input schema, `scopes_required`, a rule in `config/policy.yaml`, and a test in `tests/`. A write tool also sets `destructive=True`.
7. **Do not invent numbers.** A figure in the docs comes from a script in `scripts/` or a CI run you can link, or from a linked source. If it has not been measured, say so.
8. **No AI attribution anywhere.** No `Co-Authored-By`, no "generated with" line, in commits, PRs, docs, or comments.
9. **Do not push to `main`, tag, release, or delete branches unless asked.** Work on a branch.
10. **Docs are six files:** ARCHITECTURE, ISSUES, PRD, AGENTS, PERFORMANCE, DATA-MODEL. New knowledge goes into one of them or into `MEMORY.md`. Do not add other markdown files.

## Working rules

- Make the smallest change that does the job. Every changed line should trace to the request. Do not reformat or "improve" neighbouring code.
- Reproduce a bug with a failing test, then fix it, then show the test passing.
- Match the existing style. Docs are first person ("I chose..."), plain, and give the reason for each choice.
- Pin dependencies and images that have broken us: `mcp<2`, `elasticsearch<9`, and every image tag. Check that a tag exists before using it (MinIO's disappeared from both Docker Hub and Quay).
- Say what was verified and how. "Parsed" is not "ran". "Passes against a fake" is not "works against Postgres". A green CI run is the evidence for anything in compose.

## Commands

The virtualenv is `mcpvenv/`. Call its interpreter directly.

```bash
python3.11 -m venv mcpvenv
mcpvenv/bin/python -m pip install -e ".[dev]"   # reinstall after changing the version, or test_edges fails

mcpvenv/bin/python -m pytest -q                              # unit and in-process tests, about 4 s
mcpvenv/bin/python -m ruff check src tests deploy scripts
mcpvenv/bin/python scripts/bench_pipeline.py 1000            # request overhead, backend faked
```

Needing a running stack (CI does this; you do not run it locally):

```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose --profile seed run --rm seed
SCOUT_E2E=1 pytest tests/e2e -v
python scripts/bench_stack.py 200 10
```

`pyproject.toml` pins ruff to `E4, E7, E9, F`. Do not run `ruff --fix` with other rules selected; it rewrites unrelated files.

## CI

`.github/workflows/ci.yml`, four jobs on every push to `main` and `ship-ready` and on pull requests:

| Job | What it proves |
|-----|----------------|
| `test` | Lint, 76 tests, request-overhead benchmark |
| `validate-deploy` | Compose resolves, Prometheus rules parse and pass their unit tests, Caddyfile validates, realm and dashboard are valid JSON, Dockerfile lint |
| `stack` | Image builds, every service gets healthy, demo data loads, 31 end-to-end tests pass, then the real-backend benchmark, a memory snapshot and the image size |
| `security` | `pip-audit` on dependencies, Trivy on the Dockerfile, and Trivy on the built image, which fails on any fixable HIGH or CRITICAL finding |

To see why a run failed without waiting for the artifact: `gh run view <id> --log-failed`, or the job log through `gh api repos/<owner>/<repo>/actions/jobs/<job id>/logs`.

## Commit messages

One short imperative line, then a blank line and a paragraph if the reason is not obvious. Plain words, no trailers.

```
Load policy.yaml at startup and fail if it is missing
```

## Copilot limits (runtime)

| Limit | Value | Where |
|-------|-------|-------|
| Tools the retriever may call | `customer.build_context`, `semantic_search`, `hybrid_search`, `postgres.query`, `elasticsearch.search`, `vector.search` | `agents/retriever.py` |
| Retriever iterations | 6 | `agents/retriever.py` |
| Critic revisions | 1 | `agents/orchestrator.py` |
| Customer id | must look like `CUST-1001`, else the copilot asks for one | `agents/planner.py` |
| Writes | none; the allow-list has no write tool, and the server also enforces policy | both |
| LLM providers | one, unless `SCOUT_LLM_FALLBACK=true` | `agents/base.py` |

The server's policy applies to the copilot like any client. The client-side allow-list is a second layer, not the only one.
