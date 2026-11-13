# Product requirements

## Problem

Most MCP examples stop at a tool that returns "hello world". Putting an MCP server in front of real company data raises problems the examples skip: one customer seeing another's rows, an agent looping on a rate limit, a prompt-injected agent writing to a database, nobody able to say afterwards who asked for what.

I built Atlas MCP to show how I handled those, in code small enough to read, and to be something a team can deploy.

## Who it is for

| Reader | What they do with it |
|--------|----------------------|
| Engineer learning MCP in production | Reads the code and docs to see each concern solved in its own module, with the reason. |
| Team deploying an agent on internal data | Runs the stack, points it at their identity provider, replaces the example tools with their own. |

## Goals

1. Tenant isolation enforced in every store, so one layer failing does not leak data.
2. Deny by default: no policy rule, no access.
3. Every tool call is authenticated, authorized, rate limited, audited and traced.
4. Failures reach the agent as structured errors it can act on.
5. Destructive actions need a human to approve that exact call.
6. A new engineer can clone it, run the tests without Docker, and read how it works in an afternoon.
7. Every performance claim in the docs comes from a script in the repo or a linked source.

## Non-goals

- Issuing tokens. Atlas verifies JWTs from an identity provider; it does not replace one.
- Being a general agent framework. The copilot is one concrete example.
- Supporting every vector database or LLM provider.
- Running on the author's laptop under Docker. Integration runs belong in CI.

## Requirements and how I check them

| Requirement | Done when | Status |
|-------------|-----------|--------|
| R1 Calls from tenant A never read tenant B's data | Postgres RLS, Elasticsearch and Qdrant filters, S3 prefix, tenant in cache key | Code and in-process tests done. RLS not yet run against a real Postgres. |
| R2 Hidden tools cannot be called | `missing_scope` on a call to a tool the token does not grant | Done, tested |
| R3 Policy from YAML, deny wins, default deny | `config/policy.yaml` loaded at startup; missing file stops the server | Done, tested |
| R4 Structured errors | `code`, `retryable`, `hint` reach the client | Done, tested |
| R5 Approval for destructive tools | First call returns `pending_approval`; only the approved arguments run, once | Done, tested. No review UI or endpoint yet. |
| R6 Audit | One JSON line per call, arguments hashed | Done, tested |
| R7 Observability | Span per call, Prometheus counters, `/readyz` reflects Redis | Done, tested. Dashboards not built. |
| R8 Runs end to end from a fresh checkout | `docker compose up`, seed, ask the copilot a question | **Not met.** No token issuer in compose. |
| R9 Measured performance | Script-backed figures; real-backend figures from CI | Partly. See [PERFORMANCE.md](PERFORMANCE.md). |

## Success measures

- `pytest` and `ruff` pass with no Docker.
- The compose stack reaches healthy and the copilot answers the seeded refund question, checked in CI.
- p95 request overhead stays under 5 ms on the benchmark (0.2.0: 3.3 to 3.9 ms).
- No open item in [ISSUES.md](ISSUES.md) that loses or leaks data.

## Scope of 0.2.0

The 15 defects from the audit, plus nine found while fixing them (see ISSUES.md), the seed script, bge-base on fastembed, a request-overhead benchmark, and these docs.

## Next

Ordered by what unblocks a working deployment first:

1. A token issuer in compose, so R8 can be met.
2. A CI job that runs the stack, the seed script and an end-to-end test, which also gives real-backend benchmark numbers.
3. An endpoint for reviewing approvals.
4. Additional services in `deploy/` for security, performance and operations. I will pick these after weighing each one's cost against what it adds, and I will not list one here until I can show a number or a control it provides.
