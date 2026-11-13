# Issues

What I found when I audited the repo and then ran it for real, what I did about it, and what is still open. "Checked by" names a test in `tests/` that fails without the fix. Tests under `tests/e2e` run against the real compose stack in CI.

## Fixed

### From the first audit

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
| 9 | `SET LOCAL app.tenant_id = $1` is invalid SQL and does nothing outside a transaction | `set_config(..., true)` inside a transaction | `test_postgres_sets_tenant_inside_a_read_only_transaction`, `test_row_level_security_hides_rows_from_the_app_role` |
| 10 | The compose user is a superuser, which skips row-level security | The server connects as `atlas_app`, which cannot bypass RLS; RLS is forced | `test_row_level_security_hides_rows_from_the_app_role` |
| 11 | A `LIMIT` was always appended, so any query with its own `LIMIT` failed | Cursor fetch of `max_rows + 1` | `test_postgres_does_not_append_limit_to_the_query` |
| 12 | Elasticsearch client 9.x against an 8.14 server | `elasticsearch>=8.14,<9` | pin |
| 13 | `tickets` index and `support_docs` collection were never created; Qdrant ids could not match Postgres ids | `deploy/seed/seed.py`, `doc_id` in the payload | `test_customer_context_joins_all_four_backends` |
| 14 | Planner returned `1001` for `CUST-1001` | Returns the whole id | `test_planner_extracts_the_full_customer_id` |
| 15 | The embedding model loaded three times at startup | One lazy shared instance | `test_one_embedder_is_shared` |

### Found while fixing

| # | Problem | Fix |
|---|---------|-----|
| 16 | `mcp>=1.2.0` with no cap; 2.x removed the decorators the server uses | `mcp>=1.2,<2` |
| 17 | Policy globs treated the `*` in `tenant:*/docs/*` literally, so no per-tenant rule matched | `fnmatch` |
| 18 | The server's metrics went to one registry and `/metrics` read another | Endpoint serves the server's registry |
| 19 | `ApprovalGate.approve()` stored a field the dataclass lacked, so the next read raised | Field added |
| 20 | The tracer was configured but nothing created spans; audit `trace_id` was always null | One span per call |
| 21 | `/readyz` always said ready | 503 until startup is done and Redis answers |
| 22 | Audit, approval and retry code existed but was not called; `s3.put_object` ran with no approval | Wired into the pipeline |
| 23 | The discovery document hard-coded the issuer | Uses the configured issuer |
| 24 | Unknown tool names became metric labels, an unbounded series count | Recorded as `unknown` |

### Found by running the real stack in CI

| # | Problem | Fix | Checked by |
|---|---------|-----|------------|
| 25 | A token signed by another issuer made PyJWT raise `PyJWKClientError`, which escaped as HTTP 500 | Caught, returns 401 | `test_a_token_whose_key_is_unknown_is_rejected_not_a_server_error`, `test_a_token_for_another_audience_is_rejected` |
| 26 | Tool results holding timestamps, UUIDs or decimals could not be JSON encoded; `customer.build_context` failed | Results normalised once in the pipeline | `test_results_with_timestamps_uuids_and_decimals_are_encoded` |
| 27 | Elasticsearch's default mapping analyses text, so `term` queries on `CUST-1001` matched nothing, and a `term` filter on tenant `acme` also matches `acme-corp` | Explicit `keyword` mappings | `test_a_tenant_whose_name_starts_with_another_is_not_matched` |
| 28 | The JWKS fetch ran on the event loop | Runs in a thread | covered by the pipeline tests; not timed separately |
| 29 | S3 had no credentials and no bucket | Credentials in the environment, `s3-init` creates the bucket | `test_destructive_call_waits_for_a_human_then_runs_once` |
| 30 | Every data store was unauthenticated and published a host port | Internal network, a credential on each | `test_stores_and_the_app_are_not_reachable_from_the_host`, `test_every_store_demands_credentials` |
| 31 | `/metrics` and `/readyz` were reachable by anyone who could reach the server | Edge forwards an explicit path list | `test_edge_forwards_only_public_paths_and_sets_security_headers` |
| 32 | `http.fetch` could reach loopback and cloud-metadata addresses through an allowlisted name | Resolved address checked | `test_fetch_refuses_loopback_and_metadata_addresses` |
| 33 | LLM fallback sent customer text to several providers by default | Opt-in with `SCOUT_LLM_FALLBACK` | `test_llm_sends_prompts_to_one_provider_by_default` |
| 34 | The version was written in three places and drifted | One source, the package metadata | `test_reported_version_matches_pyproject` |
| 35 | Nothing issued a JWT, so the copilot could not authenticate | Keycloak realm, one client per persona | `test_keycloak_tokens_carry_what_scout_checks` |
| 36 | Nothing could approve a pending call | Review endpoints; the requester cannot approve | `tests/test_approvals.py`, `test_destructive_call_waits_for_a_human_then_runs_once` |
| 37 | ATBA timeouts and the circuit gauge existed but were not used | Wired into the pipeline | `tests/test_reliability_wiring.py` |
| 38 | Postgres lets every role connect to every database | `CONNECT` revoked from `PUBLIC` | `test_database_roles_are_confined_to_their_own_database` |
| 39 | The MinIO image is no longer published on Docker Hub or Quay | SeaweedFS behind the S3 API | CI stack job |
| 40 | The code said a Grafana dashboard and alert rules shipped; neither existed | Dashboard, alert rules, and `promtool` unit tests | `validate-deploy` job, `test_grafana_has_the_dashboard_and_both_datasources` |
| 41 | Caddy could not start with every capability dropped | Keep `NET_BIND_SERVICE` | CI stack job |
| 42 | The image carried 4 HIGH findings that had fixes: 2 in `zlib1g`, 2 in bundled packaging tools | Debian security updates applied, and pip, setuptools and wheel removed from the runtime image | `security` job: Trivy fails on any fixable HIGH or CRITICAL finding; it now reports 0 |

## Open

**Weaknesses I know about**
- An unknown signing key id makes PyJWT refetch the JWKS from Keycloak. A caller can send many such tokens and make Scout hammer Keycloak. A cooldown on refetches would stop it.
- `http.fetch` checks the resolved address before the request, and the request resolves again. DNS that changes in between is not covered.
- Policy conditions are substring matches: `sql_contains_any: ["DROP"]` also blocks a column called `dropped_at`. The PII check treats every identifier in the query as a requested column. Both err toward denying.
- The Postgres keyword filter is a second layer. The read-only transaction is the real guard.
- Redis is one node. If it is down, rate limiting, caching and approvals stop, and pending approvals are lost on restart. Both fail safe, but they fail.
- Traffic between containers is plain HTTP on the internal network. Only the edge uses TLS.
- Keycloak runs in `start-dev`, and the client secrets, database passwords and store credentials are development values in the repo.
- `agents/memory.py` exists and is tested alone, but the orchestrator does not use it.
- `mcp` 2.x needs a port to its new handler API.
- The real-stack benchmark moves by up to about 40% on a row between CI runs, because the runner is shared. Three runs show the spread, not its distribution. See [PERFORMANCE.md](PERFORMANCE.md).
- The official Postgres image trusts connections from `127.0.0.1` inside its own container. Only `docker compose exec` reaches it, but it means a wrong password passes there.

**Decision for you**
- Which LLM providers may see customer text. The default is one provider; `SCOUT_LLM_FALLBACK=true` adds the others listed in `agents/base.py`.
