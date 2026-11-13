# Issues

What I found when I audited the repo, what I did about it, and what is still open. "Test" names a test in `tests/` that fails without the fix.

## Fixed in 0.2.0

| # | Problem | Fix | Checked by |
|---|---------|-----|------------|
| 1 | `/mcp` crashed: the MCP session manager's `run()` was never started | Lifespan enters `session_manager.run()` | `test_mcp_endpoint_answers_without_redirect_as_event_stream` |
| 2 | `/mcp` answered 307 to `/mcp/`; the client did not follow | Plain route at `/mcp` | same |
| 3 | Client called `resp.json()` on an event stream | Client decodes JSON or SSE | `test_agent_client_lists_tools_and_calls_one` |
| 4 | Request context was a stub: every caller was anonymous, so `tools/list` returned 0 tools | Context read from the verified principal | `test_tools_list_shows_only_what_the_token_allows` |
| 5 | Server called `policy.check` with 3 arguments of 5, so every call raised `TypeError` | `Tool.authorize` passes all five | `test_call_runs_as_the_tokens_tenant` |
| 6 | `policy.yaml` conditions were mis-indented; the "block DROP" rule denied every read | Indentation fixed | `test_policy_file_allows_a_normal_select`, `test_shipped_policy_file_nests_its_conditions` |
| 7 | `policy.yaml` was never loaded; the registry used an allow-all engine | Server loads it at startup and fails if missing | `test_default_deny_applies_to_roles_without_a_rule` |
| 8 | Tool errors became a 15-line pydantic failure | Returned as `isError` with code, retryable, hint | `test_unknown_tool_returns_a_structured_error` |
| 9 | `SET LOCAL app.tenant_id = $1` is invalid SQL and does nothing outside a transaction | `set_config(..., true)` inside a transaction | `test_postgres_sets_tenant_inside_a_read_only_transaction` |
| 10 | The compose user is a superuser, which skips RLS | `atlas_app` role, forced RLS | SQL reviewed, not run (see open) |
| 11 | A `LIMIT` was always appended, so any query with its own `LIMIT` failed | Cursor fetch of `max_rows + 1` | `test_postgres_does_not_append_limit_to_the_query` |
| 12 | Elasticsearch client 9.x against a 8.14 server | `elasticsearch>=8.14,<9` | pin only |
| 13 | `tickets` index and `support_docs` collection were never created; Qdrant ids could not match Postgres ids | `deploy/seed/seed.py`, `doc_id` in payload | `test_semantic_search_hydrates_by_payload_doc_id` |
| 14 | Planner returned `1001` for `CUST-1001` | Returns the whole id | `test_planner_extracts_the_full_customer_id` |
| 15 | The embedding model loaded three times at startup | One lazy shared instance | `test_one_embedder_is_shared` |

Found while fixing:

| # | Problem | Fix |
|---|---------|-----|
| 16 | `mcp>=1.2.0` with no cap; 2.x removed the decorators the server uses | `mcp>=1.2,<2` |
| 17 | Policy globs treated the `*` in `tenant:*/docs/*` literally, so no per-tenant rule matched | `fnmatch` |
| 18 | The server's metrics went to one registry and `/metrics` read another | Endpoint serves the server's registry |
| 19 | `ApprovalGate.approve()` stored a field the dataclass lacked, so the next read raised | Field added |
| 20 | Tracer was configured but nothing created spans; audit `trace_id` was always null | One span per call |
| 21 | `/readyz` always said ready | 503 until startup is done and Redis answers |
| 22 | Audit, approval and retry code existed but was not called; `s3.put_object` ran with no approval | Wired into the pipeline |
| 23 | The discovery document hard-coded the issuer | Uses `ATLAS_AUTH_ISSUER` |
| 24 | Unknown tool names became metric labels, an unbounded series count | Recorded as `unknown` |

## Open

**Cannot run end to end yet**
- No token issuer. The server verifies JWTs against `ATLAS_AUTH_JWKS_URL`, and nothing in compose issues one, so `ATLAS_MCP_TOKEN=dev-token-change-me` is rejected. The copilot cannot reach the server on a fresh checkout. Next on my list.
- Nothing has been run against real services. Compose, the Dockerfile, `init.sql` and `seed.py` are written and parsed but not executed. The RLS and `atlas_app` role in particular are unverified until a CI job runs them against Postgres.

**Built but not connected**
- `reliability/atba.py` (timeout budgets) and `agents/memory.py`.
- `circuit_state` and `active_sessions` gauges are never set.
- No endpoint lets a human approve a pending call. `ApprovalGate.approve()` exists; until something calls it, destructive tools stay pending. That is the safe failure.

**Weaknesses I know about**
- The LLM client falls back across OpenRouter, a second OpenRouter model, and Groq. Customer text in prompts goes to each provider in turn. Decide which providers you allow before using real data.
- `http.fetch` checks the hostname against the allowlist but not the resolved address, so an allowlisted name that resolves to an internal IP is still reachable.
- Policy conditions are substring matches: `sql_contains_any: ["DROP"]` also blocks a column called `dropped_at`. The PII check treats every identifier in the query as a requested column. Both err toward denying.
- The Postgres keyword filter is a second layer. The read-only transaction is the real guard.
- `mcp` 2.x needs a port to its new handler API.
- `docs/DEPLOYMENT.md` and `docs/AGENT_SYSTEM.md` predate these changes and I have not reviewed them.

**You need to update `.env`** (I do not touch it): `ATLAS_EMBEDDING_MODEL=BAAI/bge-base-en-v1.5`, `ATLAS_POSTGRES_DSN` with `atlas_app` (see `.env.example`), and the vector dimension setting to 768.
