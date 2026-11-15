# Memory

Read this first. It is the project's working memory and stands in for CLAUDE.md: what this is, where it stands, the rules, and why things are the way they are. Update it when any of that changes.

## What this is

Scout MCP: a multi-tenant MCP server that gives agents access to Postgres, Elasticsearch, Qdrant and S3, plus a four-agent support copilot (Planner, Retriever, Synthesizer, Critic). The owner wants it to be something engineers learn from and companies deploy, and to be backed by numbers. Repo: github.com/YashhMahajan/Production-Grade-Agentic-MCP-System. It was renamed from Atlas MCP on 2026-11-13: package `scout_mcp`, env prefix `SCOUT_`, Keycloak realm `scout`, audience `scout-mcp`.

Docs in `docs/`, exactly six files: ARCHITECTURE, ISSUES, PRD, AGENTS, PERFORMANCE, DATA-MODEL. New knowledge goes into one of them or into this file. Do not add other markdown files.

## State (0.5.0, not tagged)

Part 1 of the build plan is built: the ten server features, on branch `part1-server-features`, one commit or two per feature, in `docs/ISSUES.md` 64 to 75. 293 in-process tests and 41 end-to-end tests; every feature's guarantees were checked by breaking them one at a time and confirming a test failed. Part 2 (the copilot) is not started. Not built from Part 1: `scout.whoami` and token exchange for a person's delegations (the sessions in Part 2 need `whoami`), a managed Keycloak user profile.

Things I learned on the way that are not in the code:
- Keycloak 26 puts `sub` in a token only if a mapper adds it. This realm defines only its own client scopes, so the built-in `basic` scope does not exist and `scout-human` has an explicit `oidc-sub-mapper`. Without it the server refuses the token ("missing the sub claim"). `tests/test_realm.py` guards it.
- The login form's cookies must be sent explicitly in the e2e `sign_in()`: Python's cookie jar withholds them from plain `http://localhost`, and Keycloak answers 400 "Cookie not found".
- The middleware now logs why a token was rejected (code and reason, never the token). That log line found the `sub` bug in one CI run; before it, a 401 said nothing.
- Two guard layers that cover the same hole hide each other from a mutation check. Test each layer on its own (the inbox's tenant filter and per-tenant key, the prompt's fixed tool list and read-only filter).
- Elasticsearch document ids are global, so two tenants can never share one; ingest ids are `doc_<tenant>_<slug>` and a clash is an error.

## State (0.4.0)

0.4.0 comes from branch `reliability-and-spec-2026`: a security review, the move to MCP SDK 2.x, and agent-layer work. Every change is in `docs/ISSUES.md` (43 to 63), each row with the test that guards it. v0.3.0 was tagged at e6ec00a.

- 151 unit and in-process tests pass in about 5 s with no Docker, `ruff` is clean, and 33 end-to-end tests pass against the real 12-service stack in CI.
- CI has four jobs: `test`, `validate-deploy`, `stack`, `security`. Start one on a branch with `gh workflow run ci.yml --ref <branch>`; it also runs on pull requests.
- The environment I built this in had no local dependencies and a metered connection, so the loop was edit, push the branch, read CI. That works and costs the laptop nothing. Download size matters to the owner; ask before installing.
- Real-backend numbers in `docs/PERFORMANCE.md` are from 0.3.0 and have not been re-measured on 0.4.0.
- Not in 0.4.0: the 2026-07-28 stateless handshake on Scout's own client, typed output schemas, a model evaluation (needs a model and a key; ask which model).

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
- **LLM is a two-model waterfall on one OpenRouter key** (merged 2026-11-15, PR #1): `SCOUT_LLM_MODEL=google/gemma-4-31b-it:free`, then `SCOUT_LLM_FALLBACK_MODEL=inclusionai/ling-3.2-flash:free`. Empty fallback turns it off. No fallback on 400/401/402/403. The free tier is about 50 requests a day without credit, so never make live LLM calls in tests or CI. The key is only in the gitignored `.env`; the owner asked that it be handled carefully. Free models send customer text to third-party hosts (open question 6).
- **`SCOUT_ENV=production` refuses development settings** (`ServerSettings.production_problems`). `AGENTS.md` at the root links to `docs/AGENTS.md`; `llms.txt` indexes the docs. `tests/test_hardening.py` checks every tool's scopes, approval and rate-limit names.
- **The runtime image has no pip, setuptools or wheel.** They carried advisories and nothing builds at runtime. It also gets Debian's security updates at build time.
- **Images are pinned and checked for existence first.** The Trivy setup action pinned a release that had been removed, so Trivy runs from its own pinned image.
- **Ruff pinned to `E4,E7,E9,F`** so a newer ruff does not add rules.
- **Caddy keeps `NET_BIND_SERVICE`** because it cannot exec with every capability dropped.

## Open work, in order

1. (Part 2 first: see the build plan below.) Check every table a query reads against policy (from `EXPLAIN`), and revoke `EXECUTE` on `set_config` from the role that runs agent SQL. Find out in CI whether `set_config()` in a query really moved the tenant before 0.4.0 closed it.
2. Run a model evaluation on the seed data and publish the result (needs the owner's choice of model and key).
3. Typed output schemas for the tools; the 2026-07-28 handshake in `ScoutMCPClient`.
4. Benchmark variance: more runs or a dedicated runner, a slow warehouse query, more than one replica; re-measure on 0.4.0.
5. Keycloak in production mode with real TLS; secrets out of the repo.
6. Decide which LLM providers may see customer text (the free OpenRouter models are third-party hosts).
7. Use or remove `agents/memory.py`: decided, see the build plan (delete long-term, replace short-term with `agents/session.py`).
9. Everything in the build plan below.
8. More `deploy/` services, each justified by a measured benefit or a test of its control.

## Build plan (written 2026-11-15; Part 1 is built, Part 2 is not)

The owner approved both parts. Build in batches, each merged only after CI passes. Opus 5.5 at high effort was used for the brainstorms; the owner wants planning done that way and building at the session's normal effort. Docs rules still apply: no new markdown files in `docs/`, no invented numbers, first-person voice.

### Part 1: ten server features
1. Approval inbox: `GET /approvals?state=pending` (needs `approval:review`, own tenant only, a Redis ZSET per tenant scored by expiry) and an optional `SCOUT_APPROVAL_WEBHOOK_URL`, HMAC-signed, ids only, never arguments. Fix first: `approval_decide` must also refuse `approval.delegator == principal.subject`.
2. `postgres.describe`: fixed `information_schema` SQL, each table checked against policy, PII columns hidden for roles under the PII deny rule. Saves model round trips.
3. `scout-mcp ingest --tenant acme <files>`: chunk, embed with fastembed, upsert into Elasticsearch, Qdrant and the Postgres `documents` table, delete stale chunks, record the embedding model name and warn on mismatch at startup. `seed.py` becomes a caller.
4. `config/policy_test.yaml` plus `scout-mcp policy test` and `policy check` (prints the deciding rule; needs `auth/policy.py` to return the rule id). Optional `scout.whoami` tool. Known weakness it should catch: `tenant:*/docs/*` also matches `x/docs/y`.
5. Entry-point plugins (`scout_mcp.tools` group, loaded after the built-ins) and `scout-mcp new-tool` scaffold. The scaffold prints the policy rule; it does not add it.
6. Typed `outputSchema` for every tool, starting with `customer.build_context`. `partial_errors` must be in the schema because the SDK validates `structuredContent`.
7. Containment tests: a scripted fake LLM that obeys any instruction inside `<tool_data>`, plus an adversarial seed ticket. Assert nothing is written, nothing crosses tenants, the fetch is refused. Tests the server's guarantees, not the model.
8. Human sign-in: public PKCE client, test users with a `tenant` attribute, roles. Issuer is pinned to `keycloak:8080`, so a host browser needs a hosts entry. Keycloak 26 token exchange for `act.sub` is unverified; the simpler path is the human's own token.
9. `scout-mcp tenant add <name>`: Keycloak clients through the admin API, allowlist entry, prints policy lines and secrets.
10. MCP prompts: `investigate-customer(customer_id, question)` built from the copilot rules and listing only tools the caller can see.

Dropped as not useful here: streaming/progress, usage metering, Helm, pagination cursors, MCP resources, LLM text-to-SQL, general webhooks, hash-chained audit, a client SDK.
Other brainstorm items not in the ten, still open: SDK-client e2e test through Caddy, Postgres `statement_timeout` and a `scout_agent` role without `set_config` (`SET LOCAL ROLE`), audit trail fixes (`approval_id` on the executed-call record, audit `GET /approvals/{id}`, stop sync file writes on the event loop), trim `/.well-known/mcp-server`, Alertmanager, two-replica CI, hashed lockfile and Dependabot, eval harness with record/replay, backup/restore with numbered migrations.

### Part 2: the copilot
Thesis: deterministic first (brief, changes, similar tickets, SLA all come from tools at 0 LLM calls); spend the model only on writing (reply draft, handoff summary, ambiguous triage); when the quota is gone, return the evidence pack instead of an error. Do not publish "minutes saved".

Defects in the current agent layer to fix first:
- `retriever.py` turns the model's `{"done": true, "findings": [...]}` into `Finding`s with `raw=None`, which the synthesizer can cite. Only tool results may become findings.
- Cost today: about 5 LLM calls per question on the happy path (planner, retriever tool+done, synthesizer, critic), about 20 worst case. Nothing counts calls; only tokens and time are budgeted. A daily-cap 429 burns the fallback call too.
- The planner call buys almost nothing: the customer id is a regex and `customer.build_context` is a fixed fan-out.
- `agents/memory.py`: short-term key `scout:stm:{session_id}` has no tenant or subject; long-term is a `NotImplementedError` stub; the copilot container is on the `app` network only, so it cannot reach Redis. Decision: delete long-term memory (a prompt-injected ticket could plant a false "fact"); replace short-term with `agents/session.py`, a file-backed store `~/.scout/sessions/<tenant>/<sub>/<id>.json`, mode 0600, 1 h TTL, 10 turns, keyed on the tenant and sub from the server's whoami, structured state only (ids, digests, cache keys, redacted last draft, calls used). Raw data is refetched through the server.
- `production_problems()` knows nothing about the copilot's LLM; add a guard refusing `:free` models under `SCOUT_ENV=production` without an explicit opt-in.
- Seed data is 3 tickets, subject only. Similar tickets, changes, SLA and triage need `ingest` first: about 200 synthetic tickets from a fixed-seed script (no model generation) with bodies, messages, resolutions, order events and embeddings.

Copilot features, ranked (LLM calls per use):
1. Low-call core: subcommands `brief`, `draft`, `triage`, `handoff`, `ask` instead of a planner; fixed tool recipes; `AgentRun.llm_calls` with a hard cap (default 3) and a daily ledger (default 40 of 50); no fallback on 400-403 or a daily-cap 429; redaction before any prompt; code checks replace the LLM critic (citations exist, every number/date/amount appears in cited evidence, no commitment phrases, word limit, canary and PII scan), critic only with `--strict`; output cache keyed on prompt version, model, redacted evidence and question; no key or budget gives the evidence pack with exit code 3. (0-2)
2. Customer brief: profile, tier, orders, open tickets with age, docs, `partial_errors`, fixed-rule flags. (0)
3. Draft reply with claim-level citations: JSON `{claims:[{text, cites}], needs_approval}`, code assembles and checks, one revise then flag. (1-2)
4. Queue triage: rules first (`config/triage_rules.yaml`), only undecided tickets batched 20 per call, capped at 2 calls, suggestions only. Report agreement on synthetic labels, not "accuracy". (0+)
5. SLA flags from `config/sla.yaml` per tenant and tier. (0)
6. What changed since last contact: new tool `customer.changes_since` (plugin), `order_events` and `ticket_messages` tables. (0)
7. Similar past tickets: Qdrant `tickets` collection with tenant filter, top 3 with resolutions; recall@3 on a labelled synthetic set. (0)
8. `investigate-customer` MCP prompt so Claude Desktop uses its own model. (0 Scout calls)
9. Escalation proposal through the approval inbox: new destructive `ticket.escalate`; the copilot has no `approval:review` scope and no approve code path. (0-1)
10. Handoff note: fixed fields plus one summary call; saving is a write behind approval. (0-1)
11. Follow-ups within a session, no model call. (0-1)
12. Free-text ask: one call returns a tool-call list, run in parallel, one synthesis call; SQL checked against the policy-filtered schema. (2-3)

Cut: the separate planner call, the 6-iteration retriever loop for known commands, the LLM critic on every run, long-term memory, sentiment, churn prediction, automatic sending, LLM tagging where rules work. Cut first if time runs short: tenant add CLI, free-text ask, handoff save, LLM critic.

CI verification pattern for Part 2: a counting scripted fake (`tests/fakes.py`, moved out of `test_agents.py`) asserts exact call counts per command, best and worst case; LLM errors give the evidence pack and exit 3; an identical second run makes 0 calls; a prompt scanner finds 0 fixture PII strings in recorded prompts; `scripts/bench_copilot_calls.py` publishes calls per command. A `workflow_dispatch`-only eval job with the key as a secret, 10 calls or fewer, records at most 5 replay cases. Pushes never call a real model.

### Build order
1. Part 1 item 7 (containment tests, shared fake and replay harness), then item 6 (output schemas, `customer.build_context` first).
2. Part 2 core, brief, draft (with the retriever, call-count, planner and MEMORY.md fixes above).
3. Part 1 item 4, then 5.
4. Part 1 item 3 (`ingest`, synthetic data), then Part 2 SLA, triage, changes, similar tickets.
5. Part 1 item 2, then Part 2 free-text ask.
6. Part 1 item 8 plus the self-approval fix, then Part 2 sessions.
7. Part 1 item 1, then Part 2 escalation and handoff.
8. Part 1 item 10, then the Part 2 playbook. Part 1 item 9 last.

Known doc drift still open: `atlas_app` in MEMORY and ISSUES #10 versus `scout_app` in `init.sql`. (Fixed in 0.5: the `mcp` pin in ARCHITECTURE, the test counts in README and AGENTS.md, the stale mcp 2.x port in the ISSUES open list, the Redis wording in README.) At the start of the 0.5 work the suite was 120 passing in-process tests, not the 151 this file used to say.

## Pointers

- Request path: `src/scout_mcp/server.py` `_run_pipeline`
- Policy rules: `config/policy.yaml`, engine in `auth/policy.py`
- Tool base and `authorize`: `tools/base.py`
- In-process test harness: `tests/conftest.py` (`app_client` runs the real server with Redis, embeddings and token validation faked)
- End-to-end tests: `tests/e2e/`, run only with `SCOUT_E2E=1` against a running stack
- Realm: `deploy/keycloak/scout-realm.json`; seed data: `deploy/sql/init.sql`, `deploy/seed/seed.py`
