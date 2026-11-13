# Product requirements

## Problem

Most MCP examples stop at a tool that returns "hello world". Putting an MCP server in front of real company data raises problems the examples skip: one customer seeing another's rows, an agent looping on a rate limit, a prompt-injected agent writing to a database, nobody able to say afterwards who asked for what.

I built Scout MCP to show how I handled those, in code small enough to read, and to be something a team can deploy.

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
5. Destructive actions need a different identity to approve that exact call.
6. `docker compose up` works for an engineer who has never seen the repo, and the claim is checked in CI on every push.
7. A new engineer can run the tests without Docker and read how it works in an afternoon.
8. Every performance claim in the docs comes from a script in the repo, a CI run, or a linked source.

## Non-goals

- Issuing tokens in the server. Scout verifies JWTs; Keycloak issues them.
- Being a general agent framework. The copilot is one concrete example.
- Supporting every vector database or LLM provider.
- Running Docker on the author's laptop. Integration runs belong in CI.

## Requirements and how I check them

| Requirement | Done when | Status |
|-------------|-----------|--------|
| R1 Calls from tenant A never read tenant B's data | Postgres RLS, Elasticsearch and Qdrant filters, S3 prefix, tenant in cache key | Done. Tested in-process and against the real stack, including a tenant named `acme-corp` next to `acme`. |
| R2 Hidden tools cannot be called | `missing_scope` on a call to a tool the token does not grant | Done, tested |
| R3 Policy from YAML, deny wins, default deny | `config/policy.yaml` loaded at startup; missing file stops the server | Done, tested |
| R4 Structured errors | `code`, `retryable`, `hint` reach the client | Done, tested |
| R5 Approval for destructive tools | First call returns `pending_approval`; a different identity with `approval:review` approves; only the approved arguments run, once | Done. Tested end to end with S3. |
| R6 Audit | One JSON line per call with a trace id, arguments hashed | Done. Tested against the real stack. |
| R7 Observability | Spans in Jaeger, Prometheus scrapes and loads the alert rules, Grafana dashboard provisioned | Done. All three checked in CI. |
| R8 Runs from a fresh checkout | Compose up, seed, 31 end-to-end tests pass | Met in CI. The one step CI does not run is the copilot's LLM call, which needs an API key. |
| R9 Measured performance | Script-backed figures, real-backend figures from CI | Met for three CI runs. See [PERFORMANCE.md](PERFORMANCE.md). |
| R10 Real identity | Tokens from Keycloak with tenant, roles, scopes and audience | Done. A token from another issuer is refused with 401. |
| R11 Least exposure | Only the edge and four UIs reach the host; stores are internal and need credentials | Done. Checked by connecting to each port and by probing each store without a credential. |
| R12 Supply chain | No known vulnerable dependency; no fixable HIGH or CRITICAL finding in the image | Done. `pip-audit` finds nothing, and Trivy found 4 fixable HIGH findings in the image, now 0. CI fails the build on any. |

## Success measures

Targets I set, and what CI observed.

| Measure | Target | Observed |
|---------|--------|----------|
| `pytest` and `ruff` with no Docker | pass, under 10 s | 76 tests in about 4 s |
| End-to-end tests against the real stack | all pass | 31 of 31 |
| Request overhead, backend faked (p95) | under 5 ms | 3.3 to 3.9 ms |
| Cache hit through the edge (p95) | under 10 ms | 5.2 to 8.2 ms for `postgres.query`; 6.8 to 11.2 ms for `customer.build_context`, so one run missed the target |
| `customer.build_context` on a miss (p95) | under 100 ms | 65.7 to 78.9 ms |
| Whole stack memory | under 2.5 GiB | 2.2 to 2.3 GiB |
| Errors in a 16-client run | 0 | 0 |

## Scope of 0.3.0

Keycloak, a TLS edge, an internal data network with a credential on every store, a hardened server container, approval review endpoints, ATBA timeouts and the circuit gauge, Prometheus alert rules and a Grafana dashboard, the CI workflow with 31 end-to-end tests and a real-stack benchmark, the fixes in [ISSUES.md](ISSUES.md), and the docs.

## Next

1. Put a distribution behind the benchmark variance (more runs, or a dedicated runner), try a slow warehouse query, and try more than one replica.
2. Keycloak in production mode with real TLS, and secrets from an environment or secret store instead of the repo.
3. A refetch cooldown on the JWKS so unknown key ids cannot make Scout hammer Keycloak.
4. Decide which LLM providers may see customer text.
5. Use or remove `agents/memory.py`.
6. More services in `deploy/`. I will add one only with a measured benefit or a test that proves the control it provides.
