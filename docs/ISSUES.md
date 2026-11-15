# Issues

What I found when I audited the repo and then ran it for real, and what I did about it. "Checked by" names a test in `tests/` that fails without the fix. Tests under `tests/e2e` run against the real compose stack in CI.

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
| 33 | LLM fallback sent customer text to four providers, one of them paid, by default | Two OpenRouter models on one key, fallback can be turned off | `test_llm_fallback_can_be_turned_off` |
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
| 49 | The row-level-security policies trust `app.tenant_id`, a session setting a query can change with `set_config()` | `set_config`, `current_setting`, `pg_*`, `dblink`, `lo_*`, `*_to_xml` and syntax that hides an identifier (quotes, dollar quoting, comments) are refused. This is deliberately blunt: a `--` inside a string literal is refused too, which broke the benchmark's own query until I changed it | `test_sql_that_can_change_the_tenant_or_hide_itself_is_refused` |
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

### The ten server features

| # | Problem | Fix | Checked by |
|---|---------|-----|------------|
| 64 | Nothing proved that a model which obeys a poisoned ticket still cannot write, fetch or cross tenants | A scripted model that follows every `CALL` found inside `<tool_data>` runs against the real server twice: through the copilot, where its allow-list stops the write and the fetches, and with that allow-list widened, where scopes, the outbound allowlist and the approval gate hold on their own | `test_through_the_copilot_the_write_and_the_fetches_never_leave_the_process`, `test_a_copilot_token_cannot_write_fetch_or_query_past_its_scopes`, `test_a_tenant_named_in_the_arguments_never_changes_whose_data_is_read`, `test_a_fetch_to_a_host_off_the_allowlist_is_refused_before_any_network`, `test_an_allowlisted_post_waits_for_a_human_and_does_not_run`, `test_a_write_the_token_is_allowed_to_make_is_still_held_for_approval` |
| 65 | A tool's result had no declared shape. A client that reads `outputSchema` rejects a result that does not fit, naming no tool | Every tool publishes an `outputSchema`, and the server checks each result before caching it, returning `output_schema_violation` with the tool name. `customer.build_context` declares `partial_errors` so a partial context still fits | `test_every_tool_has_a_sample_and_a_declared_output_schema`, `test_a_tool_that_breaks_its_own_schema_fails_loudly_and_is_not_cached`, `test_a_partial_context_still_fits_the_schema` |
| 66 | `tenant:*/docs/*` matched `x/docs/y` and `finance/tickets/payroll.csv`: the first `*` swallowed part of the key, so the support copilot could read any key with `docs` or `tickets` somewhere inside it (I probed three keys outside the two folders; two were readable) | The tenant and the path are matched separately, and a key with a `..` segment never matches a tenant rule. `config/policy_test.yaml` has 19 cases that between them decide all 8 rules, and CI runs them | `test_a_key_that_only_contains_docs_or_tickets_deeper_down_is_refused`, `test_every_rule_in_policy_yaml_is_the_deciding_rule_of_some_case` |
| 67 | A tool that declared no scopes could be called by anyone, which did not matter while all nine were built in, and could not be added without editing the server | `register()` enforces the contract for every tool (scopes, input and output schema, approval for writes). Other packages add tools through the `scout_mcp.tools` entry-point group, loaded after the built-ins; one that fails to load, repeats a built-in name or breaks the contract is logged, recorded in `plugin_errors` and left out. `scout-mcp new-tool` writes a package that passes | `test_a_plugin_that_breaks_the_contract_is_refused`, `test_a_plugin_cannot_replace_a_built_in`, `test_new_tool_writes_a_package_whose_tool_passes_the_contract` |
| 68 | Documents could only be loaded by editing `seed.py`, a re-run embedded everything again, nothing recorded which model made the vectors, and `hybrid_search` ranked a document that was cut into chunks by its worst chunk | `scout-mcp ingest` chunks, embeds with the server's own model, and writes Qdrant, then Postgres (as `scout_ingest`, a role that can only write `documents`), then Elasticsearch with a content hash, so a crash never marks a document done and an unchanged one is skipped. The model is recorded, and the server warns at startup if it differs. `hybrid_search` keeps a document's best chunk rank | `test_an_unchanged_document_is_not_embedded_again`, `test_a_document_that_shrinks_loses_its_stale_chunks`, `test_a_row_that_belongs_to_another_tenant_is_an_error_and_is_not_marked_done`, `test_a_server_embedding_with_another_model_is_reported`, `test_hybrid_search_ranks_a_document_by_its_best_chunk`, `test_an_ingested_document_is_found_by_its_tenant_and_nobody_else` (e2e) |
| 69 | The agent found column names by guessing in SQL and reading the error | `postgres.describe` answers from fixed catalog SQL (names are bind parameters). It leaves out columns the caller's role may not read and says how many, shows a foreign key only between two things the caller can see, and is never cached because the cache key does not hold the caller. The server passes the caller's subjects to tools in a context variable, and with none a tool describes nothing | `test_columns_the_pii_rule_hides_from_the_role_are_left_out_and_counted`, `test_the_result_is_never_cached_because_it_depends_on_who_asks`, `test_with_no_caller_identity_nothing_is_described`, `test_describe_lists_the_real_schema_with_foreign_keys` (e2e) |
| 70 | The human an agent acts for could approve what the agent asked on their behalf: `approval_decide` compared only the approver to the requesting agent | The identities on the request (caller, delegator) and on the decision (subject, delegator) may not overlap | `test_the_human_an_agent_acts_for_cannot_approve_its_requests`, `test_an_agent_acting_for_the_requester_cannot_approve_either` |
| 71 | Reviewing needed only the `approval:review` scope, which a sign-in client can offer to everyone | It needs the scope and `role:reviewer`, which comes from the identity provider's record of the person | `test_the_review_scope_without_the_reviewer_role_is_not_enough` |
| 72 | A reviewer needed the approval id from the agent or a log, and nobody was told a request was waiting. Caddy also forwarded `/approvals/*` but not `/approvals` | `GET /approvals?state=pending` lists what is waiting in the reviewer's own tenant, soonest to expire first, from a sorted set per tenant. It shows only what the reviewer could decide and leaves out the arguments. `SCOUT_APPROVAL_WEBHOOK_URL` sends one HMAC-signed POST per new request with ids and the tool name only, never holds up the agent, and needs a secret | `test_a_reviewer_sees_what_is_waiting_soonest_to_expire_first`, `test_the_list_never_carries_the_arguments`, `test_a_new_request_is_announced_with_ids_only_and_a_signature`, `test_a_webhook_that_is_down_does_not_fail_or_hold_up_the_call`, `test_the_call_returns_while_the_webhook_is_still_in_flight`, `test_the_inbox_lists_a_waiting_request_through_the_edge_without_its_arguments` (e2e) |
| 73 | Only agents had identities; a person could not sign in | `scout-human` is a public client with S256 PKCE required and no secret. A person's tenant is a user attribute only an admin sets, mapped into the token, and their roles come from realm roles the client may carry | `test_people_sign_in_with_pkce_and_the_token_says_who_they_are`, `test_a_sign_in_without_a_code_challenge_is_refused`, `test_a_person_sees_only_their_own_tenants_rows`, `test_a_person_cannot_approve_their_own_request_but_a_reviewer_can` (all e2e, through the real authorization-code flow) |
| 74 | A tenant's three Keycloak clients were written by hand, and a typo in a hard-coded `tenant` claim reads another tenant's data | `scout-mcp tenant add` builds a copilot, a writer and a reviewer from one definition. An existing client stops the run before any change, and a failure part way removes what was made | `test_a_generated_client_matches_the_realm_files_own_for_acme_field_for_field`, `test_every_generated_client_carries_the_new_tenant_and_no_other` |
| 75 | The copilot's way of working was only usable through Scout's own model calls | The `investigate-customer` MCP prompt carries the copilot's rules for a client's own model, and names only the read-only tools that caller can see | `test_it_names_only_tools_that_caller_can_see_and_never_a_write_tool`, `test_a_write_tool_handed_to_the_builder_is_still_not_named` |
