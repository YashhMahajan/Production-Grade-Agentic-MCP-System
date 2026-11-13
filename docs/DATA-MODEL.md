# Data model

Every store, what is in it, and how tenancy is enforced there. Seed data is in `deploy/sql/init.sql` (Postgres) and `deploy/seed/seed.py` (Elasticsearch and Qdrant). Two demo tenants: `acme` and `globex`.

## Postgres

Two databases on one server: `scout` (this section) and `keycloak` (owned by its own `keycloak` role; `scout_app` has no access to it).

Tables: `customers`, `orders`, `documents`. All have `tenant_id TEXT NOT NULL`.

| Table | Key columns |
|-------|-------------|
| `customers` | `id` (e.g. `CUST-1001`), `name`, `email`, `tier`, `created_at` |
| `orders` | `id` (e.g. `o_9002`), `customer_id` -> customers, `status`, `total_cents`, `currency`, `created_at` |
| `documents` | `id` (e.g. `doc_refund_policy`), `title`, `body`, `url`, `created_at` |

Isolation:

- RLS is enabled and **forced** on all three. Policy: `tenant_id = current_setting('app.tenant_id', true)`. If the setting is missing the comparison is NULL and no rows come back.
- Two roles. `scout` (the compose superuser) creates the schema. `scout_app` has `SELECT` on the three tables, `NOSUPERUSER NOBYPASSRLS`. The server must connect as `scout_app`: superusers skip RLS entirely.
- `postgres.query` runs in a read-only transaction and calls `set_config('app.tenant_id', $1, true)` first. The `true` makes it transaction-local.

## Tokens (Keycloak)

What the server reads from a verified access token:

| Claim | Meaning |
|-------|---------|
| `sub` | The agent's identity, recorded as `caller` in the audit log |
| `tenant` | The tenant every query is pinned to |
| `roles` | Space-separated policy subjects, for example `role:support_copilot` |
| `scope` | Space-separated tool scopes, for example `tool:postgres:read` |
| `aud` | Must include `scout-mcp` |
| `act.sub` | Optional: the human who delegated to the agent, recorded as `delegator` |

Realm clients: `copilot-acme`, `copilot-globex` (read scopes), `writer-acme` (`s3` read and write), `reviewer-acme` (`approval:review`). Their secrets in the realm file are for development.

## Elasticsearch

| Index | Fields | Notes |
|-------|--------|-------|
| `tickets` | `customer_id` (keyword), `status` (keyword), `priority` (keyword), `subject` (text), `created_at` (date), `_tenant` (keyword) | `status` is `open` or `pending` in the seed data |
| `documents` | `title` (text), `body` (text), `_tenant` (keyword) | mirror of the Postgres `documents` rows, `_id` is the document id |

`_tenant`, `customer_id` and `status` must be mapped as `keyword`. With the default dynamic mapping they become analysed text, exact `term` queries stop matching, and a `term` filter on `_tenant: acme` also matches a tenant called `acme-corp`. `deploy/seed/seed.py` creates the indices with explicit mappings; do the same in any real index.

The tool wraps every query: `bool.must = [caller query]`, `bool.filter = [term _tenant]`. The cluster has security on: the server and the seed script authenticate as `elastic`.

## Qdrant

One collection, `support_docs`: cosine distance, 768 dimensions (bge-base-en-v1.5). Point ids are UUIDs derived from the document id, because Qdrant only accepts integers or UUIDs. Payload:

| Field | Meaning |
|-------|---------|
| `doc_id` | The Postgres and Elasticsearch document id. Composed tools join on this. |
| `tenant` | Pinned by a mandatory `must` clause on every search. |
| `title`, `text` | For previews without a database round trip. |

Every request carries the `api-key` header. Changing the embedding model changes the dimension. Re-run the seed script and re-embed any real data, or searches return noise.

## Redis

Password required. Memory capped at 256 MB with `volatile-lru`, no snapshots: every key has a TTL and losing them on restart fails safe.

| Key | Holds | TTL |
|-----|-------|-----|
| `scout:<tool>:<24 hex>` | cached tool result, hash of tool + tenant + arguments | per tool, L2 default 600 s |
| `scout:rl:<tenant>:<tool>` | token bucket: `tokens`, `last_ms` | 3600 s |
| `scout:approval:<id>` | pending approval: tenant, caller, delegator, tool, arguments, state, approver | 3600 s, deleted when used |
| `<cache key>:lock` | stampede lock | 5 s |

## S3 (SeaweedFS in compose)

Bucket `scout-mcp-data`, created by the `s3-init` service. Object key is `<tenant>/<key>`. A tenant cannot name another tenant's prefix because the server adds its own.

## Audit log

Newline-delimited JSON at `SCOUT_AUDIT_LOG_PATH`, one line per tool call, also printed to stdout. Fields: `ts`, `trace_id`, `tenant`, `caller`, `delegator`, `tool`, `args_hash`, `duration_ms`, `status`, `error_code`. Arguments are hashed with SHA-256 and never written, because they can hold personal data.

## Metrics

| Name | Labels |
|------|--------|
| `scout_tool_calls_total` | `tool`, `status` (`ok` or an error code) |
| `scout_tool_latency_seconds` | `tool` |
| `scout_cache_hits_total`, `scout_cache_misses_total` | `tool` |
| `scout_rate_limited_total` | `tool` |
| `scout_circuit_state` | `tool` (0 closed, 1 half-open, 2 open) |

No tenant or caller labels: they multiply the series count. Unregistered tool names are recorded as `unknown`.
