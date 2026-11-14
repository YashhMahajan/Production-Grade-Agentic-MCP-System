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

### Found by reading the code with a security reviewer's eye

I went through auth, tenant isolation, the approval gate, `http.fetch`, the paths that carry customer text into the agent, and the HTTP transport. Each row has a test that fails without the fix.

| # | Problem | Fix | Checked by |
|---|---------|-----|------------|
| 43 | An approval was checked, then the tool ran, then the approval was deleted. Five calls sent at once with one approved id all ran the tool | Spending an approval is a `SET NX` claim made before the tool runs; one caller wins, the rest get `approval_already_used` | `test_one_approval_runs_the_call_once_when_spent_five_times_at_once` |
| 44 | Two reviewers could approve and deny at once; the last write won. Every decision also reset the request's one-hour TTL | Decisions are `SET NX` too, and take the request's remaining TTL | `test_two_reviewers_deciding_at_once_get_one_winner`, `test_a_decision_does_not_extend_the_requests_lifetime` |
| 45 | A call the circuit breaker refused still used up its approval | The claim is released when the breaker refuses; the backend was never called | `test_an_approval_survives_a_call_the_circuit_breaker_refused` |
| 46 | `http.fetch` POST, PUT, PATCH and DELETE ran with no approval | Every method except GET waits for a human | `test_http_fetch_get_runs_but_every_other_method_waits_for_a_human` |
| 47 | Calls that change state were retried, and a retried write can run twice | Calls that need approval run once | `test_one_approval_runs_the_call_once_when_spent_five_times_at_once` (the tool runs once) |
| 48 | `collection` and `index` went into a URL path and an index expression unchecked, so `../x` or `a?b` could steer the request at another Qdrant endpoint | Names are validated against a short pattern | `test_collection_names_cannot_steer_the_request_path`, `test_index_names_cannot_reach_system_or_multiple_indices` |
| 49 | The row-level-security policies trust `app.tenant_id`, a session setting a query can change with `set_config()` | `set_config`, `current_setting`, `pg_*`, `dblink`, `lo_*`, `*_to_xml` and syntax that hides an identifier (quotes, dollar quoting, comments) are refused | `test_sql_that_can_change_the_tenant_or_hide_itself_is_refused` |
| 50 | The keyword check looked for `" DELETE "` between spaces, so a newline or tab in front of it got past | Word-boundary match | same test, the `\n` and `\t` cases |
| 51 | A token with no `tenant` claim joined a shared `default` tenant | Rejected with `missing_tenant` (`SCOUT_REQUIRE_TENANT=false` restores the old behaviour for single-tenant use) | `test_a_token_without_a_tenant_is_rejected` |
| 52 | No `Origin` check on the HTTP transport, which the spec requires (403) | `OriginMiddleware`; browser origins must be listed in `SCOUT_ALLOWED_ORIGINS` | `test_a_browser_origin_is_refused_and_a_client_without_one_is_not`, edge e2e test |
| 53 | No OAuth protected-resource metadata, so a standard MCP client could not learn which server issues tokens | `/.well-known/oauth-protected-resource` (RFC 9728), and `resource_metadata` in every 401 | `test_protected_resource_metadata_names_the_authorization_server` |
| 54 | A `customer.build_context` result with `partial_errors` was cached for 30 s, so one backend blip was served to everyone | Results with `partial_errors` are not cached | `test_partial_results_are_not_cached_but_complete_ones_are` |
| 55 | `http.fetch` checked an address, then connected to a second DNS answer | It connects to the address it checked; `Host` and TLS name stay the real host; a caller-supplied `Host` is dropped; `::ffff:127.0.0.1` is unwrapped before the loopback check | `test_fetch_connects_to_the_address_it_checked`, the `::ffff:` cases |
| 56 | An unknown signing key id made Scout refetch the JWKS from Keycloak on every request | One refetch per cooldown (`SCOUT_AUTH_JWKS_REFETCH_COOLDOWN_S`, 30 s), starting once one key has verified a token. My first version applied it from startup, and the real-stack run showed why that is wrong: a wrong-realm token arriving first used up the cooldown and every real token got a 401 | `test_unknown_key_ids_trigger_one_jwks_fetch_per_cooldown` |
| 57 | Redis trouble turned cache and rate-limit calls into `internal_error`, and a Redis restart broke the rate limiter until Scout restarted (`NOSCRIPT`) | A cache that is down is a miss; the limiter falls back to a per-replica bucket and reloads its script; an unreachable approval store returns a retryable `approvals_unavailable`; Redis calls time out after `SCOUT_REDIS_TIMEOUT_S` | `test_a_cache_that_is_down_is_a_miss_not_an_error`, `test_a_rate_limiter_that_loses_redis_limits_locally`, `test_a_rate_limiter_reloads_its_script_after_redis_restarts`, `test_an_approval_store_that_is_down_says_so_and_runs_nothing` |

### The agent layer

| # | Problem | Fix | Checked by |
|---|---------|-----|------------|
| 58 | Ticket subjects, documents and fetched pages went into the model's prompt as if they were instructions | Tool output is wrapped in `<tool_data id=...>` with an id that is new for every call, and the prompts say it is data | `test_tool_results_reach_the_model_inside_a_block_the_data_cannot_close` |
| 59 | The token counter existed and nothing used it | A run has a token budget (`SCOUT_AGENT_TOKEN_BUDGET`, 60,000) and a time limit (`SCOUT_AGENT_TIMEOUT_S`, 90); a run that hits one returns a degraded answer with what it had | `test_the_token_budget_stops_the_run_before_the_next_tool_call`, `test_the_time_budget_is_enforced_and_reported` |
| 60 | One reply that was not JSON ended the run | One repair attempt; the retriever counts an unreadable reply as an iteration; a plan that cannot be read falls back to the question; an unreadable critic is a rejection | `test_one_reply_that_is_not_json_is_asked_again` and the three after it |
| 61 | Nothing checked that `[S7]` pointed at a finding | The orchestrator checks citations in code, whatever the critic said | `test_a_citation_to_a_finding_that_does_not_exist_is_never_approved` |
| 62 | The retriever chose tools from a name and a sentence, never the argument names | The prompt lists each tool's arguments and which are required | `test_the_retriever_is_told_each_tools_arguments` |
| 63 | The client threw away `retry_after_seconds`, and the server never learned how long the agent had left | The client waits out a rate limit once (at most 5 s), and sends the run's remaining time as `X-Scout-Deadline-Ms`, which the server treats as a hard cap | `test_the_client_waits_out_a_rate_limit_once_and_sends_the_deadline`, `test_a_deadline_from_the_caller_cuts_a_slow_tool_short` |

## Open

**Weaknesses I know about**
- The Postgres policy resource is the first table after `FROM`. A query that joins or sub-selects another table is checked against the first one only, so the `analyst` rule (`analytics/*`) does not stop an analyst reading `customers` in their own tenant. Row-level security still limits them to their tenant. The fix I would make is to read the real relations from `EXPLAIN (FORMAT JSON)` and check each one.
- The `*` in a policy resource matches `/`, so `tenant:*/docs/*` also matches `tenant:acme/x/docs/y`. The S3 read rule is wider than it looks.
- The `pii_fields` rule reads column names out of the SQL text, so `SELECT *` does not trip it. It protects nothing today only because the seed schema has no such columns.
- A circuit breaker belongs to a tool, not a backend. When Postgres is down, `customer.build_context` keeps calling it and reports `partial_errors`.
- I could not run Postgres to test whether `set_config()` in a query really moves the tenant (#49), so I closed the route instead. Revoking `EXECUTE` on `set_config` from the role that runs agent SQL would close it for good; I have not done that.
- Policy conditions are substring matches: `sql_contains_any: ["DROP"]` also blocks a column called `dropped_at`. The PII check treats every identifier in the query as a requested column. Both err toward denying.
- The Postgres keyword filter is a second layer. The read-only transaction is the real guard.
- Redis is one node. If it is down, caching and rate limiting degrade (every read is a miss; each replica limits on its own), but approvals stop, `/readyz` reports not ready, and pending approvals are lost on restart.
- Traffic between containers is plain HTTP on the internal network. Only the edge uses TLS.
- Keycloak runs in `start-dev`, and the client secrets, database passwords and store credentials are development values in the repo.
- `agents/memory.py` exists and is tested alone, but the orchestrator does not use it.
- `mcp` 2.x needs a port to its new handler API (`on_list_tools`, `on_call_tool`, the request from `ctx.request`), and with it the 2026-07-28 spec: no `initialize`, `server/discover`, `resultType`.
- The real-stack benchmark moves by up to about 40% on a row between CI runs, because the runner is shared. Three runs show the spread, not its distribution. See [PERFORMANCE.md](PERFORMANCE.md).
- The official Postgres image trusts connections from `127.0.0.1` inside its own container. Only `docker compose exec` reaches it, but it means a wrong password passes there.

**Decision for you**
- Which LLM providers may see customer text. The default is one provider; `SCOUT_LLM_FALLBACK=true` adds the others listed in `agents/base.py`.
