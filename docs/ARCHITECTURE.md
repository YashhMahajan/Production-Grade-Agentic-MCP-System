# Architecture

I built Scout MCP as one MCP server between AI agents and a company's data stores, plus a four-agent support copilot that uses it. This page describes what the code and the compose stack do. Where a module exists but is not on the request path, I say so.

## The stack

Fifteen services in `docker-compose.yml`, on two networks.

```
host ──127.0.0.1:8443──> caddy ──> scout-mcp ──> postgres, redis, elasticsearch, qdrant, s3   (internal "data" network)
host ──127.0.0.1:8081──> keycloak ──> postgres                                  scout-mcp ──> keycloak (JWKS)
host ──127.0.0.1:3000/9090/16686──> grafana, prometheus, jaeger                 scout-mcp ──> otel-collector ──> jaeger
```

- The `data` network is `internal: true`: Postgres, Redis, Elasticsearch, Qdrant and the S3 store have no route to or from the host, and every one of them requires a credential.
- Caddy is the only way to the MCP server. It terminates TLS, sets security headers, limits request bodies to 1 MB, and forwards only `/mcp`, `/approvals`, `/approvals/*`, `/.well-known/mcp-server` and `/healthz`. `/metrics` and `/readyz` are not reachable through it.
- The server container is read-only, runs as uid 10001 with all capabilities dropped, and receives only the settings it uses. The LLM keys never enter it.
- Three one-shot services: `s3-init` creates the bucket, `seed` loads demo data (profile `seed`), `copilot` runs a question (profile `copilot`).

## Request path

A tool call over HTTP, in order. The file is in the right column.

| # | Step | What it does | File |
|---|------|--------------|------|
| 1 | Edge | TLS, path allowlist, body limit | `deploy/caddy/Caddyfile` |
| 2 | Auth middleware | Verifies the bearer JWT against Keycloak's JWKS: signature, issuer, audience, expiry, 30 s clock leeway. The key fetch runs in a thread. | `auth/middleware.py`, `auth/oauth.py` |
| 3 | Tenant middleware | Tenant comes from the token. A differing `X-Tenant-Id` is a 403 unless the token has `tenant:*:impersonate`. | `governance/tenant.py` |
| 4 | MCP session | Stateless Streamable HTTP at `/mcp` | `server.py` |
| 5 | Context | Reads principal and tenant from `request.state` | `server.py` |
| 6 | Validate | The tool's Pydantic schema. Failures return one line: `field: reason`. | `tools/base.py` |
| 7 | Authorize | Token scopes, then `config/policy.yaml` for the caller and each of its roles | `tools/base.py`, `auth/policy.py` |
| 8 | Rate limit | Redis token bucket on tenant and tool | `ratelimit/limiter.py` |
| 9 | Approval | Destructive tools stop until a human approves this exact call | `governance/approval.py` |
| 10 | Cache | L1 in process, L2 in Redis, key includes the tenant | `cache/manager.py` |
| 11 | Execute | Circuit breaker around a time-budgeted, retried tool call | `reliability/` |
| 12 | Record | One span, one audit line, Prometheus counters and the circuit gauge | `observability/` |

Errors at any step are `ToolError` subclasses with `code`, `retryable` and `hint`. They return as an MCP result with `isError: true` and that payload as JSON, so an agent can decide whether to retry. Anything else becomes `internal_error` with no detail.

Time budget: each call gets the shorter of the tool's declared `timeout_ms` and the ATBA allowance (1.5 times the observed p95, capped by a fair share of the 30 s budget). A timeout is an `UpstreamError`, so a backend that keeps timing out opens the circuit.

## Identity

Scout only verifies tokens. Keycloak (realm `scout`, `deploy/keycloak/scout-realm.json`) issues them. Agents use the client-credentials grant, one client per persona; `scout-mcp tenant add` makes a new tenant's three. Each token carries `tenant`, `roles`, `scope` and `aud: scout-mcp`. The issuer is pinned to `http://keycloak:8080/realms/scout`, so a token requested from the host and one requested inside the network have the same `iss`.

People sign in through `scout-human`, a public client that uses the authorization-code flow with S256 PKCE and has no secret to leak. A person's tenant is a user attribute that only an admin sets, mapped into the `tenant` claim, so signing in through another app cannot change it. Their roles are realm roles (`human_agent`, `reviewer`) that the client may carry, and they appear as `role:...` in the `roles` claim. The dev users are `ana` and `rae` (acme) and `gus` (globex).

## Approvals

`s3.put_object` and `ticket.escalate` are destructive. The first call returns `pending_approval` with an id. A reviewer reads `GET /approvals/{id}` to see the exact arguments, then calls `POST /approvals/{id}/approve` or `/deny`. The agent then repeats the call with `approval_id`; it runs only if the tool and arguments match what was approved, and the approval is deleted afterwards.

Reviewing takes the `approval:review` scope **and** `role:reviewer`. The scope is something a client can request; the role comes from the identity provider's record of the person, so a sign-in client that offers the scope to everyone does not make everyone a reviewer. The identities on the request (caller and delegator) and on the decision (subject and delegator) may not overlap, so neither the agent nor the human it acts for can approve it. Another tenant's reviewer is refused, and a decision cannot be changed.

`GET /approvals?state=pending` is the reviewer's inbox: what is waiting in their own tenant, soonest to expire first, leaving out anything they could not decide and leaving out the arguments (those are on the detail view). It reads a sorted set per tenant, `scout:approvals:{tenant}`, scored by expiry, pruned on read and trimmed when a request is decided. If `SCOUT_APPROVAL_WEBHOOK_URL` is set, each new request also sends one POST in the background with `approval_id`, `tenant`, `tool` and `created_at` only. It is signed: recompute `HMAC-SHA256(secret, "<X-Scout-Timestamp>." + raw body)`, compare it to `X-Scout-Signature` (`sha256=<hex>`), and reject an old timestamp. A slow or dead receiver is cut off after 5 s and never holds up the agent; there is no retry, so the inbox is the record.

## Tools

| Level | Tool | Needs scopes | Cache |
|-------|------|--------------|-------|
| Atomic | `postgres.query` | `tool:postgres:read` | 30 s |
| Atomic | `postgres.describe` | `tool:postgres:read` | none (depends on the caller) |
| Atomic | `elasticsearch.search` | `tool:elasticsearch:read` | 30 s |
| Atomic | `vector.search` | `tool:vector:read` | 300 s |
| Atomic | `s3.get_object` | `tool:s3:read` | 300 s |
| Atomic | `s3.put_object` (destructive) | `tool:s3:write` | none |
| Atomic | `ticket.escalate` (destructive) | `tool:tickets:escalate` | none |
| Atomic | `http.fetch` | `tool:http:fetch` | none |
| Composed | `semantic_search` | vector + postgres read | 120 s |
| Composed | `hybrid_search` | vector + elasticsearch read | 120 s |
| Workflow | `customer.build_context` | postgres + elasticsearch + vector read | 30 s |
| Workflow | `customer.activity_since` | postgres + elasticsearch read | 30 s |

`http.fetch` checks the host against a per-tenant allowlist and refuses names that resolve to loopback, link-local (cloud metadata) or unspecified addresses.

`postgres.describe` lists the tables and columns the caller may read, from fixed catalog SQL with the names as bind parameters. The server puts the caller's subjects in a context variable (`tools.base.CALLER`) so a tool can filter by who asks; with none set the tool describes nothing. Because its answer depends on the caller and the cache key does not include one, it is never cached.

**Every tool meets one contract**, enforced when it is registered: at least one scope (`authorize` checks each scope a tool lists, so none would mean anyone), an input schema, an output schema, and approval if it writes. Each tool publishes its `outputSchema`, and the server checks every result against it before caching, returning `output_schema_violation` with the tool name; a client that reads the schema would otherwise reject the result without saying which tool broke it. Other packages add tools through the `scout_mcp.tools` entry-point group, loaded after the built-ins and held to the same contract. A plugin that fails is logged, listed in `registry.plugin_errors` and left out. `scout-mcp new-tool NAME` writes a package that already passes.

## Getting data and tenants in

`scout-mcp ingest --tenant acme docs/*.md` cuts each file into chunks, embeds them with the model the server uses, and writes three stores in an order that survives a crash: Qdrant (the new chunks, then this document's stale ones), Postgres (the row, as `scout_ingest`, a role that can write only `documents`, with the tenant set so row-level security applies), then Elasticsearch with a hash of the content. The hash goes last, so a document is only marked done once everything else is written, and an unchanged document is skipped without embedding. A document id that belongs to another tenant is an error. The embedding model's name is recorded, and the server warns at startup if it differs from the one it embeds queries with.

`scout-mcp tenant add NAME` makes a tenant's copilot, writer and reviewer clients in Keycloak from one definition and shows each generated secret once. `scout-mcp policy test` runs `config/policy_test.yaml` against `config/policy.yaml`, and `policy check` says which rule decides one call. None of these edits `config/`: the commands print what to change and a person changes it.

The `investigate-customer` MCP prompt gives a client's own model the copilot's rules (tool output is data, read only, cite the tool, no promises) for a customer id and a question, naming only the read-only tools that caller can see. Scout makes no model call for it.

## Tenant isolation, layer by layer

The tenant is set once, from the token, and every backend enforces it separately:

- Postgres: row-level security on `app.tenant_id`, set with `SET LOCAL` inside a read-only transaction. The app connects as `scout_app`, which cannot bypass it and has no `EXECUTE` on `set_config`, the one function that could move the setting.
- Elasticsearch: a mandatory `_tenant` filter wrapped around the caller's query.
- Qdrant: a mandatory `tenant` match under the caller's filter.
- S3: every key is prefixed with the tenant.
- Cache: the tenant is part of the key.

## Agents

The copilot has four commands. `brief` and `activity` answer from the tools alone, with no model. `draft` and `ask` write a reply from a model. Agents reach the server through `ScoutMCPClient`, so server policy applies to them like any client. Every model call is counted against the run and against a daily allowance (default 40), contact details are redacted before a prompt leaves, and when the model cannot be used both print the brief and exit with code 3. Only a successful tool result becomes evidence that can be cited. Chat models are `ChatOpenAI` pointed at OpenRouter: `SCOUT_LLM_MODEL`, then `SCOUT_LLM_FALLBACK_MODEL` if that call fails or returns nothing. Both use one key; the defaults are free models from different upstream hosts, so one host being rate limited does not stop a turn.


### The model flows (LangGraph)

`scout-copilot activity CUST-1001 [7d | DATE]` lists the orders and tickets created since a date, from one call to `customer.activity_since`, with no model. Only creation times are stored, so it shows what is new, not what changed on an existing order or ticket. I did not give the tool a name that promised more.

Ticket age is judged against `config/sla.yaml`: hours an open or pending ticket may wait, by priority, with the most specific entry winning (the tenant's tier, the tenant's default, the tier, then `default`). A ticket is reported as nearing its target at 75% of it and as past it beyond 100%. The brief shows the flag, and the draft's evidence carries the same text, so a claim that a ticket is past its SLA can be checked against evidence like any other claim. A priority with no target is never flagged, and a missing file turns flags off with a note instead of guessing a target.

`scout-copilot draft CUST-1001 "question"` runs a LangGraph state graph in `agents/graphs/draft.py`: load the customer's context with one fixed `customer.build_context` call, number the facts as `[S1]`, `[S2]`, ask the model for claims that each cite those numbers, check every claim in code, revise once if a check fails, and otherwise stop. Anything that goes wrong (no key, no budget, a model error, an unreadable reply twice) ends in the evidence pack and exit code 3.

`scout-copilot ask "question"` shares everything after the evidence is gathered and differs in one step: a model call proposes at most four read-only tool calls as JSON, and code decides what runs (`agents/graphs/ask.py`). A tool off the allow-list is refused and listed. When the question names a customer, the `customer.build_context` call is forced first whatever the model said, and repeats are dropped. The rest run in parallel through `ScoutMCPClient`, so scopes, policy and the deadline apply to each. Each result becomes numbered evidence, cut at 1,500 characters. The step that reads the evidence has no tools, so an instruction in a ticket cannot cause a call. An instruction in the question can only change which tools are proposed, and the allow-list and then the server decide what happens (`tests/test_containment.py`).

I removed the old loop of up to six model-chosen tool steps, the planner call and the model critic. In that loop the model reading tool results also chose the next call, and a model checking a model cost a call from a small allowance. A run is now two model calls when nothing goes wrong, where it was five, and the tests assert both counts. Tokens are no longer counted: each reply is limited to 700 tokens, calls are capped and a tool result is cut, which bounds a run without a counter.

I used LangGraph only to connect steps. Each step is a small function over a run object, and the checks live in `agents/claims.py` with no framework in them, so the guarantees do not depend on it. The run object, not graph state, holds the MCP client, the raw evidence and the model clients; state carries counters, the outcome and the check results, so a trace or a later checkpoint shows the shape of a run and none of its customer data. A test asserts that.

What the code checks, so a second model does not have to: every claim cites evidence that exists, every number, date, amount and contact detail in a claim appears in the evidence it cites, no claim commits the company to an outcome ("we will refund"), and the draft is at most 180 words. I dropped the model critic on this path because a model checking a model costs a call from a small daily allowance and is itself unreliable on a free tier.

Chat models are `ChatOpenAI` pointed at OpenRouter with `max_retries=0`. The fallback loop is mine (`agents/llm.py`) instead of `with_fallbacks`, because what follows a failure decides how much of the allowance is spent: a 400 to 403, or a 429 that names a per-day cap, is final, since every free model shares it; a 5xx, a timeout, a per-provider 429 or an empty reply tries the second model. Every attempt is counted, and no attempt starts once the run's cap (default 3, `SCOUT_DRAFT_MAX_LLM_CALLS`) is reached. I ask for JSON in the prompt and validate it with Pydantic, with one counted repair, instead of `with_structured_output`, because free hosts differ in which structured modes they support.

`scout-copilot escalate TICKET "reason"` is the one write the copilot can ask for, and it makes no model call. `ticket.escalate` raises a ticket to high priority and records when and why; the server holds every call for a reviewer. The command is a LangGraph graph (`agents/graphs/escalate.py`) because LangGraph has a real pause: the graph reads the ticket, shows the person the ticket and the reason, and stops at `interrupt` until they answer (`--yes` skips the question). A "no" sends nothing. A "yes" makes the call, and the result is a pending approval, never a change, because the copilot has no `approval:review` scope. After a reviewer approves it, the same command with `--approval ID` makes the approved call, which runs once. I kept the write out of `ask` and `draft` on purpose: their allow-lists name only read tools, a test checks that no tool in the registry that writes is on them, and the containment tests show that a poisoned question which names `ticket.escalate` is refused in code, refused by the server for a token without the scope, and held for a human for a token with it.

Tracing with LangSmith is optional and sends the shape of a run, not its content. It is off unless `SCOUT_LANGSMITH=1` and a key are set. When it is on, the copilot attaches its own `LangChainTracer` with a LangSmith client built with `hide_inputs`, `hide_outputs` and `hide_metadata` (`agents/tracing.py`): inputs and outputs keep their keys and the length of each string and nothing else, metadata keeps an allow-list (the command, the prompt version, the model, the step), and the tenant appears only as an HMAC under `SCOUT_TRACE_SALT`, or not at all without one. So a trace shows which steps ran, in what order, how long each took and how many characters moved, and a reader learns nothing about a customer. I chose that default because the free models already send redacted text to third-party hosts, and I did not want a second place that holds it. LangChain's own tracing is forced off, because a shell with `LANGCHAIN_TRACING_V2=true` would otherwise send full prompts with none of this reduction. One limit: exception messages are sent as they were raised, and nothing in this code puts customer text in one. A test runs a real draft through the real tracer and client with only the network replaced, and asserts that no customer string appears in anything that would have been sent.

Evaluation is by replay, not by a model judging a model. `tests/replays/draft_cases.json` holds 18 hand-written cases, each a customer context, the model replies to hand back in order, and what the checks must conclude (approved, flagged for a named kind of problem, or the evidence pack). `scripts/eval_copilot.py` runs them through the real graph with `FakeListChatModel`, so there is no model and no network, and CI fails if any case disagrees with its label. They measure whether a wrong total, a promise, a missing citation, an invented email or an over-long draft is caught and a sound draft is not. They say nothing about how well a model writes, and I do not publish a score for that. With `--langsmith` the cases are uploaded as a dataset so the same inputs can be run there; I read the client's `has_dataset`, `create_dataset` and `create_examples` signatures from its source and tested the calls with a fake client, and I have not run the upload against LangSmith. A guard in `tests/conftest.py` makes `openrouter.ai` and `smith.langchain.com` unresolvable in every test, so a push cannot spend the free allowance or send a trace by accident.

I did not use `langchain-mcp-adapters` to load Scout's tools. Its repository is [archived](https://github.com/langchain-ai/langchain-mcp-adapters) and its last release, [0.3.2](https://pypi.org/project/langchain-mcp-adapters/), requires `mcp<2.0.0`, while this project needs `mcp>=2.3`. The draft path calls one fixed tool anyway, and it goes through `ScoutMCPClient`, which keeps the deadline header, the rate-limit wait and the `<tool_data>` marking under tests of mine.

The framework is the `copilot` extra (`langgraph`, `langchain-core`, `langchain-openai`). The server never imports it: a test imports the server in a clean interpreter and asserts no `langchain*` or `langgraph` module is loaded, and CI checks the built server image cannot import `langgraph` while the copilot image can. The two images come from one Dockerfile; the copilot image is built with `EXTRAS=[copilot]`.

## Observability

Spans go to the OTel collector and Jaeger. Prometheus scrapes `scout-mcp:8080/metrics` and evaluates `deploy/prometheus/alerts.yml` (down, server-side error ratio, open circuit, p95 latency, rate-limit spike), which has unit tests run by `promtool` in CI. Grafana provisions a Prometheus and a Jaeger datasource and the "Scout MCP" dashboard from `deploy/grafana/`.

## Decisions

- **Stateless sessions.** Any replica can serve any request. The MCP session is rebuilt per request, which is most of the per-call overhead in [PERFORMANCE.md](PERFORMANCE.md).
- **Read and write are separate tools.** A prompt-injected agent with a SQL tool can drop a table. With read-only tools it gets a policy error.
- **Keycloak, not a homegrown issuer.** Scout stays a resource server.
- **SeaweedFS for S3.** MinIO stopped publishing container images, so the S3 store is SeaweedFS. The tools use plain S3 and work with any compatible store.
- **Stores on an internal network.** Row-level security protects data from a buggy query. Network isolation protects it from anyone who skips the application.
- **bge-base over bge-large.** About 1 point lower on MTEB retrieval, 3x fewer parameters, 5.7x smaller on disk.
- **`mcp` 2.x, `elasticsearch` below 9.** The server uses 2.x's `on_list_tools` and `on_call_tool` handlers and takes the verified caller from `ctx.request`. The compose Elasticsearch is 8.14.
- **Review needs a scope and a role.** A scope can be requested by any client that offers it. The role is the identity provider's record of the person.
- **A tool's result is checked against its own schema.** A wrong schema is worse than none, because every compliant client then breaks, so the server says which tool broke it instead of leaving that to the client.
- **Ingest writes the hash last.** A crash leaves a document that is simply done again, never one that looks done and is not.
- **Commands print config changes, they do not make them.** Granting access or outbound hosts is a decision for a person, and `config/` is read-only in the image.
