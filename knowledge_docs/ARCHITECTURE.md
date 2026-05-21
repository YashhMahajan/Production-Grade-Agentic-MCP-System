# Architecture

Atlas-MCP is organised as **twelve components** that sit in a layered
request pipeline. This document walks through the request path end to end,
then unpacks each layer.

## The request path

A tool call enters the server via Streamable HTTP (or stdio for local),
traverses the middleware stack, and lands in the dispatch function in
`server.py`. Every step below is enforced in code; nothing is optional:

```
  HTTP request
       │
       ▼
  [1] Transport layer           ← server.py  (Streamable HTTP / stdio)
       │
       ▼
  [11] Tracing middleware       ← observability/tracing.py  (OTel span begins)
       │
       ▼
  [2] Auth middleware           ← auth/middleware.py       (validates JWT)
       │
       ▼
  [12] Tenant middleware        ← governance/tenant.py     (extracts tenant)
       │
       ▼
  MCP call_tool handler         ← server.py
       │
       ▼
  [5] Input validation          ← tools/base.py            (Pydantic schema)
       │
       ▼
  [3] Policy engine             ← auth/policy.py           (deny-by-default)
       │
       ▼
  [8] Rate limiter              ← ratelimit/limiter.py     (Redis token bucket)
       │
       ▼
  [9] Cache read                ← cache/manager.py         (L1 → L2 → miss)
       │                                    (hit) ──► return cached result
       ▼ (miss)
  [7] Circuit breaker           ← reliability/circuit_breaker.py
       │
       ▼
  [6] Tool execution            ← tools/{atomic,composed,workflow}/
       │
       ▼
  [10] Error normalisation      ← errors/framework.py      (SERF → MCP wire)
       │
       ▼
  [11] Audit log + metrics      ← observability/{audit,metrics}.py
       │
       ▼
  MCP response
```

The ordering is not negotiable:

- **Tracing first** because every other layer should be able to add span
  attributes describing its decision.
- **Auth before tenancy** because tenancy is derived from the Principal.
- **Validation before policy** because a malformed payload is rejected before
  we waste a policy evaluation on it.
- **Policy before rate limit** because a denied call should not consume quota.
- **Rate limit before cache** because a cache hit for a banned caller is
  still a leak.
- **Cache before circuit breaker** because a warm cache can serve during a
  downstream outage.

## Components in depth

### 1. Transport & Session Layer — `server.py`

Two transports supported from day one: stdio for local hosts (Claude Desktop,
Cursor) and Streamable HTTP for remote deployments. The Streamable HTTP
session manager is configured in **stateless mode** so that any replica can
handle any request — the 2026 MCP roadmap's top production fix. Session data
(if any) lives in Redis.

### 2. Authentication — `auth/oauth.py`

Atlas is a **resource server**, not an authorization server. It only
validates JWTs against a JWKS endpoint. The authorization server (WorkOS
AuthKit, Auth0, Descope, or your own) handles login, consent, client
registration, and token issuance. This separation is load-bearing: if the
MCP server issues its own tokens you end up reinventing identity.

Three MCP-specific claims we expect on the token:

- `tenant` — multi-tenancy scope.
- `act.sub` (RFC 8693 actor claim) — the human who authorised the agent.
- `scope` — space-separated list like `tool:postgres:read tool:vector:read`.

### 3. Authorization — `auth/policy.py`

YAML-driven rules with a narrow DSL (`sql_contains_any`, `pii_fields`).
Deny-by-default. Deny rules beat allow rules. Resources are globbed with a
`tenant:<id>/<path>` convention so every rule carries tenancy in its match.

The engine is boring on purpose. A policy DSL that can express anything is
one that nobody understands six months later.

### 4. Tool Registry & Discovery — `tools/registry.py`

- In-memory index for `list_tools` / `call_tool`.
- Visibility filter: a caller never sees a tool whose required scopes they
  do not hold. Cleaner context window ⇒ better tool choice by the LLM.
- `/.well-known/mcp-server` endpoint publishes capability metadata without
  requiring a live session — part of the 2026 MCP roadmap.

### 5. Input Validation — `tools/base.py` + per-tool schemas

Every tool declares a Pydantic model. `SELECT`-only guards in Postgres
tool, HTTPS-only in the HTTP tool, enum constraints on methods, regex on
customer ids. Agent input is the default threat model. Raise
`ValidationError` with a one-line hint the agent can actually learn from.

### 6. Tool Execution Engine — three-level hierarchy

| Level | Example | Purpose |
|-------|---------|---------|
| **Atomic** | `postgres.query` | One primitive, one backend |
| **Composed** | `semantic_search`, `hybrid_search` | Small deterministic chains |
| **Workflow** | `customer.build_context` | Multi-step procedure behind one name |

Giving agents a higher-level option measurably reduces erroneous calls
(~40% in the April 2026 production case study with 87 tools). The atomic
layer stays available for edge cases.

### 7. Reliability — `reliability/`

- **Circuit breaker** — per-tool, closed / half-open / open, counts only
  retryable upstream errors. Stops the cascade when one backend dies.
- **Retry** — exponential backoff with full jitter, honours
  `ToolError.retryable`, respects the ATBA deadline so it never retries
  past budget.
- **ATBA (Adaptive Timeout Budget Allocation)** — one total budget per
  agent turn, divided across tool calls by observed p95. Guarantees bounded
  end-to-end latency even when an agent chains many calls.

### 8. Rate Limiting — `ratelimit/limiter.py`

Redis token bucket driven by a Lua script so check-and-consume is atomic.
Keyed on `(tenant, tool)`. Per-tool overrides let you give expensive
workflow tools tighter limits than atomic reads. An agent burst that would
exhaust quota gets a `RateLimitError` with a `retry_after_seconds` hint.

### 9. Caching — `cache/manager.py`

Two tiers (L1 in-process LRU, L2 shared Redis), write-through. Stampede
prevention via a short Redis NX lock so a thundering herd of agents on the
same cold key triggers exactly one compute. Keys are deterministic hashes
of `(tool, tenant, args)` so tenants cannot collide.

### 10. Structured Error Framework — `errors/framework.py`

Every surfaced error is a `ToolError` subclass with `code`, `retryable`,
`hint`, `context`. Converted to MCP's on-the-wire `ErrorData` so hosts that
understand Atlas can parse the hint and hosts that do not still get a
readable message. A Python traceback teaches an agent nothing; a SERF
payload tells it whether to retry and what to try differently.

### 11. Observability — `observability/`

- **Tracing (OTel)** — one span per tool call with tool, tenant, cache
  outcome, circuit state. Exported via OTLP to an OTel collector that fans
  out to Jaeger / Datadog / your SIEM.
- **Metrics (Prometheus)** — counters and latency histograms labelled by
  tool (not tenant — that is a cardinality bomb). Grafana reads from
  `/metrics`.
- **Audit log** — newline-delimited JSON, one line per call, `args_hash`
  (never raw args — PII risk), trace id, tenant, caller, delegator. Your
  SIEM owns durability.

### 12. Governance & Multi-Tenancy — `governance/`

- **Tenant middleware** — attaches the Principal's tenant to
  `request.state`. Optional header-based impersonation gated by a scope
  that only internal admins hold.
- **Approval gate** — destructive tools do not execute immediately. They
  create a Redis-backed pending approval and return `PendingApprovalError`.
  A human reviews and approves via a separate dashboard. This is what lets
  you ship write-capable agents without betting the company.
- **Row-level security** — Postgres RLS policies pinned to
  `current_setting('app.tenant_id')`. Even if a policy bug lets an agent
  run an unfiltered query, the database physically cannot return another
  tenant's rows.

## The agent layer

`src/atlas_mcp/agents/` is where Atlas becomes a product. The support
copilot is a deliberately simple four-agent orchestration:

```
  user question
       │
       ▼
  [Planner]   — emits a retrieval plan JSON
       │
       ▼
  [Retriever] — tool-calling loop via AtlasMCPClient (read-only tools only)
       │
       ▼
  [Synthesizer] — drafts a reply with [S1]-style citations
       │
       ▼
  [Critic]    — approves or sends back to Synthesizer once
       │
       ▼
  response (approved | needs human review)
```

Bounded loops, explicit roles, one `AgentRun` object that carries token
counts and tool calls across all four agents. Everything debuggable; nothing
magical.
