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
- Caddy is the only way to the MCP server. It terminates TLS, sets security headers, limits request bodies to 1 MB, and forwards only `/mcp`, `/approvals/*`, `/.well-known/mcp-server` and `/healthz`. `/metrics` and `/readyz` are not reachable through it.
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

Scout only verifies tokens. Keycloak (realm `scout`, `deploy/keycloak/scout-realm.json`) issues them through the client-credentials grant, one client per persona. Each token carries `tenant`, `roles`, `scope` and `aud: scout-mcp`. The issuer is pinned to `http://keycloak:8080/realms/scout`, so a token requested from the host and one requested inside the network have the same `iss`.

## Approvals

`s3.put_object` is destructive. The first call returns `pending_approval` with an id. A reviewer holding the `approval:review` scope reads `GET /approvals/{id}` to see the exact arguments, then calls `POST /approvals/{id}/approve` or `/deny`. The requester cannot approve its own call, another tenant's reviewer is refused, and a decision cannot be changed. The agent then repeats the call with `approval_id`; it runs only if the tool and arguments match what was approved, and the approval is deleted afterwards.

## Tools

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

`http.fetch` checks the host against a per-tenant allowlist and refuses names that resolve to loopback, link-local (cloud metadata) or unspecified addresses.

## Tenant isolation, layer by layer

The tenant is set once, from the token, and every backend enforces it separately:

- Postgres: row-level security on `app.tenant_id`, set inside a read-only transaction. The app connects as `scout_app`, which cannot bypass it.
- Elasticsearch: a mandatory `_tenant` filter wrapped around the caller's query.
- Qdrant: a mandatory `tenant` match under the caller's filter.
- S3: every key is prefixed with the tenant.
- Cache: the tenant is part of the key.

## Agents

Planner, Retriever, Synthesizer, Critic, run by `SupportCopilot` in `agents/orchestrator.py`. The retriever may call six read-only tools, at most 6 iterations. The critic can send the draft back once. Agents reach the server through `ScoutMCPClient`, so server policy applies to them like any client. The LLM client sends prompts to one provider unless `SCOUT_LLM_FALLBACK=true`.

## Observability

Spans go to the OTel collector and Jaeger. Prometheus scrapes `scout-mcp:8080/metrics` and evaluates `deploy/prometheus/alerts.yml` (down, server-side error ratio, open circuit, p95 latency, rate-limit spike), which has unit tests run by `promtool` in CI. Grafana provisions a Prometheus and a Jaeger datasource and the "Scout MCP" dashboard from `deploy/grafana/`.

## Decisions

- **Stateless sessions.** Any replica can serve any request. The MCP session is rebuilt per request, which is most of the per-call overhead in [PERFORMANCE.md](PERFORMANCE.md).
- **Read and write are separate tools.** A prompt-injected agent with a SQL tool can drop a table. With read-only tools it gets a policy error.
- **Keycloak, not a homegrown issuer.** Scout stays a resource server.
- **SeaweedFS for S3.** MinIO stopped publishing container images, so the S3 store is SeaweedFS. The tools use plain S3 and work with any compatible store.
- **Stores on an internal network.** Row-level security protects data from a buggy query. Network isolation protects it from anyone who skips the application.
- **bge-base over bge-large.** About 1 point lower on MTEB retrieval, 3x fewer parameters, 5.7x smaller on disk.
- **`mcp` pinned below 2, `elasticsearch` below 9.** Version 2 of `mcp` removed the decorator API the server uses. The compose Elasticsearch is 8.14.

## Not on the request path

`agents/memory.py` exists and is tested in isolation, but the orchestrator does not use it. See [ISSUES.md](ISSUES.md).
