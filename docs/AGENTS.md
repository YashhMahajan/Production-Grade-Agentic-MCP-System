# Agent rules

Rules for any AI assistant or script working in this repo, then the limits the copilot itself runs under. If a rule here conflicts with a request, stop and ask.

## Hard guardrails

1. **Do not start Docker, Compose, or any container, VM, or large download on the developer's machine.** Check compose files and Dockerfiles statically (YAML parses, `ruff`, `docker compose config` in CI). Anything that needs a running container runs in GitHub Actions: push a branch and read the run.
2. **Never print or commit values from `.env`.** It is gitignored and holds real keys. You may edit it when the owner asks. Mirror every new variable in `.env.example` with a safe development value.
3. **Do not weaken isolation or deny-by-default.** Never remove a tenant filter, widen `scout_app`'s grants, let the server connect as a superuser, set `SCOUT_POLICY_DEFAULT_DENY=false`, or add a policy rule without a test that shows who it allows.
4. **Keep the data stores private and authenticated.** Postgres, Redis, Elasticsearch, Qdrant and the S3 store stay on the internal `data` network with no published ports, and each keeps its password or key. Do not publish a port on anything except `127.0.0.1`.
5. **Give each service only the secrets it uses.** `scout-mcp` lists its settings under `environment:`; do not give it `env_file: .env`, which would put the LLM keys in the server container.
6. **A new tool needs five things:** a Pydantic input schema, an output schema, `scopes_required`, a rule in `config/policy.yaml` with a case in `config/policy_test.yaml`, and a test in `tests/`. A write tool also sets `destructive=True`. `register()` refuses a tool that lacks a scope, a schema or approval for a write, and `scout-mcp new-tool` writes one that passes.
7. **Do not invent numbers.** A figure in the docs comes from a script in `scripts/` or a CI run you can link, or from a linked source. If it has not been measured, say so.
8. **No AI attribution anywhere.** No `Co-Authored-By`, no "generated with" line, in commits, PRs, docs, or comments.
9. **Do not push to `main`, tag, release, or delete branches unless asked.** Work on a branch.
10. **Docs are six files:** ARCHITECTURE, ISSUES, PRD, AGENTS, PERFORMANCE, DATA-MODEL. New knowledge goes into one of them or into `MEMORY.md`. Do not add other markdown files.

## Working rules

- Make the smallest change that does the job. Every changed line should trace to the request. Do not reformat or "improve" neighbouring code.
- Reproduce a bug with a failing test, then fix it, then show the test passing.
- Match the existing style. Docs are first person ("I chose..."), plain, and give the reason for each choice.
- Pin dependencies and images that have broken us: `mcp<3`, `elasticsearch<9`, and every image tag. Check that a tag exists before using it (MinIO's disappeared from both Docker Hub and Quay).
- Say what was verified and how. "Parsed" is not "ran". "Passes against a fake" is not "works against Postgres". A green CI run is the evidence for anything in compose.

## Commands

The virtualenv is `mcpvenv/`. Call its interpreter directly.

```bash
python3.11 -m venv mcpvenv
mcpvenv/bin/python -m pip install -e ".[dev]"   # reinstall after changing the version, or test_edges fails

mcpvenv/bin/python -m pytest -q                              # unit and in-process tests, about 4 s
mcpvenv/bin/python -m ruff check src tests deploy scripts
mcpvenv/bin/python scripts/bench_pipeline.py 1000            # request overhead, backend faked
mcpvenv/bin/python -m scout_mcp.cli policy test --strict      # config/policy_test.yaml against config/policy.yaml
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

`.github/workflows/ci.yml`, four jobs on every push to `main` and `ship-ready` and on pull requests, and a fifth, `secrets`, on pull requests only. The token is read-only (`permissions: contents: read`) and every action is pinned to a commit SHA:

| Job | What it proves |
|-----|----------------|
| `test` | Lint, 505 tests, the policy cases (`scout-mcp policy test --strict`), the copilot replay evals, request-overhead benchmark |
| `validate-deploy` | Compose resolves, Prometheus rules parse and pass their unit tests, Caddyfile validates, realm and dashboard are valid JSON, Dockerfile lint |
| `stack` | Image builds, every service gets healthy, demo data loads, a sample file is ingested for acme (and ingesting it again embeds nothing), 44 end-to-end tests pass, then the real-backend benchmark, a memory snapshot and the image size |
| `secrets` | `gitleaks` over the commits a pull request adds, with the development-only files in `.gitleaks.toml` allowed; fails on a credential |
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
| Tools `ask` may call | `customer.build_context`, `customer.activity_since`, `semantic_search`, `hybrid_search`, `postgres.query`, `postgres.describe`, `elasticsearch.search`, `vector.search`; anything else is refused in code and listed | `agents/graphs/ask.py` |
| Tool calls per `ask` | 4, after the customer lookup is forced first and repeats are dropped; they run in parallel | `agents/graphs/ask.py` |
| Tools `draft` may call | one fixed `customer.build_context`; the model chooses none | `agents/graphs/draft.py` |
| Tools in the step that reads evidence | none, so an instruction in a ticket cannot cause a call | `agents/graphs/core.py` |
| Model calls per run | `ask` 6 (`SCOUT_AGENT_MAX_LLM_CALLS`), `draft` 3 (`SCOUT_DRAFT_MAX_LLM_CALLS`), counting the plan, the repair, the revision and each fallback attempt; no attempt starts past the cap | `agents/graphs/core.py`, `agents/llm.py` |
| Model calls per day | 40 (`SCOUT_LLM_DAILY_LIMIT`), kept in `~/.scout/ledger.json` (mode 0600); when spent, `ask` and `draft` print the brief and exit 3 | `agents/ledger.py`, `agents/cli.py` |
| Size of a run | each reply is limited to 700 tokens and a tool result is cut to 1,500 characters; tokens are not counted | `agents/llm.py`, `agents/graphs/ask.py` |
| Time per run | 90 s (`SCOUT_AGENT_TIMEOUT_S`); the time left goes to the server on every tool call, and no model call starts after it | `agents/graphs/core.py`, `agents/mcp_client.py` |
| A reply that is not JSON | one repair attempt (counted), then the run ends with the evidence pack | `agents/graphs/core.py` |
| Revisions | 1; a draft that still fails its checks is flagged, not approved | `agents/graphs/core.py` |
| Checks on a draft | every claim cites existing evidence; numbers, dates, amounts and contact details appear in the cited evidence; no commitment phrase; at most 180 words | `agents/claims.py` |
| Evidence | only successful tool results become evidence; a denial, a refusal or an error is listed, never cited; what the model writes is never evidence | `agents/graphs/ask.py` |
| Tool results | wrapped in `<tool_data id=...>` with an id that is new every call, so the data cannot close the block | `agents/mcp_client.py` |
| Redaction | emails, phone numbers and card-like numbers become placeholders before a prompt leaves and are restored in the reply; names and free text are not caught | `agents/redact.py` |
| Customer id | must look like `CUST-1001`; `draft` and `activity` refuse without one, `ask` continues without a customer | `agents/ids.py` |
| Writes | one: `ticket.escalate`, which only the `escalate` command may call, as a request the server holds for a reviewer; the copilot has no `approval:review` scope and no approve path. The allow-lists of `ask` and `draft` have no write tool, and a test checks every tool in the registry against them | `agents/graphs/escalate.py`, `agents/graphs/ask.py`, `tools/atomic/ticket_escalate.py` |
| `escalate` | no model call; shows the ticket and reason and waits for a yes (`--yes` skips the question); a no sends nothing; after approval, `--approval ID` makes the approved call once | `agents/graphs/escalate.py`, `agents/cli.py` |
| LLM models | primary `SCOUT_LLM_MODEL`, then `SCOUT_LLM_FALLBACK_MODEL` on an error or empty reply, but not on a 400-403 or a 429 that names a per-day cap; both free on OpenRouter, same key. With `SCOUT_ENV=production` a `:free` model is refused unless `SCOUT_ALLOW_FREE_LLM=1` | `agents/llm.py`, `agents/cli.py` |
| Tracing | off unless `SCOUT_LANGSMITH=1` and `LANGSMITH_API_KEY` are set; sends keys and string lengths, never text; metadata from an allow-list; the tenant only as an HMAC under `SCOUT_TRACE_SALT`; LangChain's own tracing is forced off | `agents/tracing.py` |
| Replay evals | 18 hand-written cases run through the real draft graph with replayed model replies, no model and no network; `scripts/eval_copilot.py` exits 1 if any case disagrees with its label (CI runs it); `--langsmith` uploads the cases as a dataset. They measure the graph and its checks, not any model | `agents/evals.py`, `tests/replays/draft_cases.json` |
| Tests and hosts | no test can resolve `openrouter.ai` or `smith.langchain.com`; a push never calls a model or sends a trace | `tests/conftest.py` |
| SLA flags | targets from `config/sla.yaml` (`SCOUT_SLA_FILE`): nearing at 75% of the target, past beyond it; open and pending tickets only; no target means no flag | `agents/sla.py` |
| `activity` | one `customer.activity_since` call and no model; creation times only | `agents/cli.py` |
| `brief` | no model call and no LLM key; fixed-rule flags only | `agents/brief.py` |

The server's policy applies to the copilot like any client. The client-side allow-list is a second layer, not the only one.
