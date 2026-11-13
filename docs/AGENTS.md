# Agent rules

Rules for any AI assistant or script working in this repo, then the limits the copilot itself runs under. If a rule here conflicts with a request, stop and ask.

## Hard guardrails

1. **Do not start Docker, Compose, or any container, VM, or large download on the developer's machine.** Compose files and the Dockerfile are checked statically (YAML parses, `ruff`). Real stack runs happen in CI.
2. **Do not read, print, edit, or commit `.env`.** It holds real keys. Change `.env.example` and tell the owner which lines to update.
3. **Do not weaken isolation or deny-by-default.** Never remove a tenant filter, widen `atlas_app`'s grants, let the server connect as a superuser, set `ATLAS_POLICY_DEFAULT_DENY=false`, or add a policy rule without a test that shows who it allows.
4. **A new tool needs four things:** a Pydantic input schema, `scopes_required`, a rule in `config/policy.yaml`, and a test in `tests/`. A write tool also sets `destructive=True`.
5. **Do not invent numbers.** A figure in the docs comes from `scripts/` output you ran, or a linked source. If you have not measured it, say so.
6. **No AI attribution anywhere.** No `Co-Authored-By`, no "generated with" line, in commits, PRs, docs, or comments.
7. **Do not push, tag, release, or delete branches unless asked.**

## Working rules

- Make the smallest change that does the job. Every changed line should trace to the request. Do not reformat or "improve" neighbouring code.
- Reproduce a bug with a failing test, then fix it, then show the test passing.
- Match the existing style. Docs are first person ("I chose..."), plain, and give the reason for each choice.
- Pin dependencies that have broken us (`mcp<2`, `elasticsearch<9`). Check a new major version before allowing it.
- Say what was verified and how. "Parsed" is not "ran". "Passes against a fake" is not "works against Postgres".

## Commands

The virtualenv is `mcpvenv/`. Call its interpreter directly; do not rely on activation.

```bash
python3.11 -m venv mcpvenv
mcpvenv/bin/python -m pip install -e ".[dev]"

mcpvenv/bin/python -m pytest -q                    # all tests, no Docker, about 3 s
mcpvenv/bin/python -m ruff check src tests deploy scripts
mcpvenv/bin/python scripts/bench_pipeline.py 1000  # request overhead

```

`pyproject.toml` pins ruff to `E4, E7, E9, F`. Do not run `ruff --fix` with other rules selected; it rewrites unrelated files.

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

The server's policy applies to the copilot like any client. The client-side allow-list is a second layer, not the only one.
