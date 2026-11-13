# Architecture

I built Atlas MCP as one MCP server that sits between AI agents and a company's data stores, plus a four-agent support copilot that uses it. This page describes what the code does today. Where something exists as a module but is not on the request path, I say so.

## Request path

One tool call over HTTP goes through these steps, in this order. Each step is a function you can open: the file is in the right column.

| # | Step | What it does | File |
|---|------|--------------|------|
| 1 | Auth middleware | Verifies the bearer JWT against the issuer's JWKS. Public paths skip it. | `auth/middleware.py`, `auth/oauth.py` |
| 2 | Tenant middleware | Takes the tenant from the token. An `X-Tenant-Id` header that differs is a 403 unless the token has `tenant:*:impersonate`. | `governance/tenant.py` |
| 3 | MCP session manager | Stateless Streamable HTTP, mounted at `/mcp`. | `server.py` |
| 4 | Context | Reads the principal and tenant the middleware stored on `request.state`. | `server.py` `_current_context` |
| 5 | Validate | The tool's Pydantic schema. Failures return one line: `field: reason`. | `tools/base.py` |
| 6 | Authorize | Token scopes first, then `config/policy.yaml` for the caller and each of its roles. | `tools/base.py`, `auth/policy.py` |
| 7 | Rate limit | Redis token bucket keyed on tenant and tool. | `ratelimit/limiter.py` |
| 8 | Approval | Destructive tools stop here until a human approves this exact call. | `governance/approval.py` |
| 9 | Cache | L1 in process, L2 in Redis. The key includes the tenant. | `cache/manager.py` |
| 10 | Execute | Circuit breaker, around retry, around the tool. | `reliability/` |
| 11 | Record | One OTel span, one audit line, Prometheus counters. | `observability/` |

Errors at any step are `ToolError` subclasses with `code`, `retryable` and `hint`. They go back as an MCP result with `isError: true` and that payload as JSON, so an agent can decide whether to retry. Anything that is not a `ToolError` becomes `internal_error` with no detail.

Public paths (no token): `/healthz`, `/readyz`, `/metrics`, `/.well-known/mcp-server`. I made `/metrics` public because Prometheus does not send tokens. Do not expose it outside your cluster.

## Tools

Three levels. Agents are better off calling one composed tool than assembling three atomic ones.

| Level | Tool | Needs scopes | Cache |
|-------|------|--------------|-------|
| Atomic | `postgres.query` | `tool:postgres:read` | 30 s |
| Atomic | `elasticsearch.search` | `tool:elasticsearch:read` | 30 s |
| Atomic | `vector.search` | `tool:vector:read` | 300 s |
| Atomic | `s3.get_object` | `tool:s3:read` | 300 s |
| Atomic | `s3.put_object` (destructive) | `tool:s3:write` | none |
| Atomic | `http.fetch` | `tool:http:fetch` | none |
| Composed | `semantic_search` | vector + postgres read | 120 s |
| Composed | `hybrid_search` | vector + elasticsearch read | 120 s |
| Workflow | `customer.build_context` | postgres + elasticsearch + vector read | 30 s |

## Tenant isolation, layer by layer

A tenant id is set once, from the token, and every backend enforces it on its own:

- Postgres: row-level security on `app.tenant_id`, which the tool sets inside a read-only transaction. The app connects as `atlas_app`, which cannot bypass RLS.
- Elasticsearch: a mandatory `_tenant` term filter wrapped around the caller's query.
- Qdrant: a mandatory `tenant` match under the caller's filter.
- S3: every key is prefixed with the tenant.
- Cache: the tenant is part of the key.

## Agents

Planner, Retriever, Synthesizer, Critic, run by `SupportCopilot` in `agents/orchestrator.py`. The retriever may call six read-only tools, at most 6 iterations. The critic can send the draft back once. The agents talk to the server through `AtlasMCPClient`, so the server's policy applies to them like any other client.

## Decisions

- **Stateless sessions.** Any replica can serve any request. The cost is that the MCP session is rebuilt per request, which is most of the 2.7 ms in [PERFORMANCE.md](PERFORMANCE.md).
- **Atlas is a resource server only.** It validates tokens and never issues them.
- **Read and write are separate tools.** A prompt-injected agent with a SQL tool can drop a table. With read-only tools it gets a policy error.
- **bge-base over bge-large.** About 1 point lower on MTEB retrieval, 3x fewer parameters, 5.7x smaller on disk. Numbers and sources are in PERFORMANCE.md.
- **`mcp` is pinned below 2.** Version 2 removed the decorator API the server uses.

## Not on the request path yet

These modules exist and are tested in isolation but nothing calls them: `reliability/atba.py` (timeout budgets), `agents/memory.py`, and the `circuit_state` and `active_sessions` gauges. See [ISSUES.md](ISSUES.md).
