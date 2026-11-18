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
- Three roles. `scout` (the compose superuser) creates the schema. `scout_app` has `SELECT` on the three tables, `NOSUPERUSER NOBYPASSRLS`. The server must connect as `scout_app`: superusers skip RLS entirely. `scout_ingest` has `SELECT, INSERT, UPDATE` on `documents` and nothing else, also `NOBYPASSRLS`; only `scout-mcp ingest` uses it, through `SCOUT_INGEST_POSTGRES_DSN`, and it sets `app.tenant_id` before it writes.
- `postgres.query` and `postgres.describe` run in a read-only transaction and run `SET LOCAL app.tenant_id = '<tenant>'` first, with the name checked against the one shape a tenant may have (`tenant_sql.py`). `set_config()` is refused to every role but a superuser (`REVOKE EXECUTE ... FROM PUBLIC` in `init.sql`), so a query that got past the SQL filter still could not point the setting at another tenant, and a `SELECT` cannot issue `SET`. `init.sql` only runs on a new volume, so on an existing database run `REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC;` once as a superuser, in the `scout` database. `postgres.describe` reads `information_schema.columns` (which lists only tables the role can read) and `pg_constraint` for foreign keys (`information_schema` hides constraints on tables the role does not own).

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

An agent's `roles` and `tenant` are hard-coded claims on its client. A person's token comes from `scout-human`: `tenant` is the user's `tenant` attribute and `roles` is a list of their realm roles with the prefix `role:`. `sub` is their Keycloak id and there is no `act`.

Realm clients: `copilot-acme`, `copilot-globex` (read scopes), `writer-acme` (`s3` read and write), `reviewer-acme` (`approval:review`, `role:reviewer`), and `scout-human` (public, PKCE). Users: `ana` (acme, `human_agent`), `rae` (acme, `reviewer`), `gus` (globex, `human_agent`). Their secrets and passwords in the realm file are for development.

## Elasticsearch

| Index | Fields | Notes |
|-------|--------|-------|
| `tickets` | `customer_id` (keyword), `status` (keyword), `priority` (keyword), `subject` (text), `created_at` (date), `_tenant` (keyword) | `status` is `open` or `pending` in the seed data |
| `documents` | `title` (text), `body` (text), `_tenant` (keyword), `_sha` (keyword) | mirror of the Postgres `documents` rows, `_id` is the document id. `_sha` is the hash of the content ingest last wrote, and is written last |
| `scout_meta` | `model`, `dim` | one document per collection, `embedding-<collection>`: which model embedded it |

`_tenant`, `customer_id` and `status` must be mapped as `keyword`. With the default dynamic mapping they become analysed text, exact `term` queries stop matching, and a `term` filter on `_tenant: acme` also matches a tenant called `acme-corp`. `deploy/seed/seed.py` creates the indices with explicit mappings; do the same in any real index.

The tool wraps every query: `bool.must = [caller query]`, `bool.filter = [term _tenant]`. The cluster has security on: the server and the seed script authenticate as `elastic`.

## Qdrant

One collection, `support_docs`: cosine distance, 768 dimensions (bge-base-en-v1.5). A document is cut into chunks, one point each. Point ids are UUIDs derived from the tenant, the document id and the chunk number, because Qdrant only accepts integers or UUIDs and two tenants must never share one. Payload:

| Field | Meaning |
|-------|---------|
| `doc_id` | The Postgres and Elasticsearch document id. Composed tools join on this. |
| `chunk` | The chunk number within the document. Ingest removes this tenant's chunks at or above the new count when a document gets shorter. |
| `tenant` | Pinned by a mandatory `must` clause on every search. |
| `title`, `text` | For previews without a database round trip. `text` is the chunk, not the whole document. |

Every request carries the `api-key` header. Changing the embedding model changes the dimension. Re-run the seed script and re-embed any real data, or searches return noise.

## Redis

Password required. Memory capped at 256 MB with `volatile-lru`, no snapshots: every key has a TTL and losing them on restart fails safe.

| Key | Holds | TTL |
|-----|-------|-----|
| `scout:<tool>:<24 hex>` | cached tool result, hash of tool + tenant + arguments | per tool, L2 default 600 s |
| `scout:rl:<tenant>:<tool>` | token bucket: `tokens`, `last_ms` | 3600 s |
| `scout:approval:<id>` | pending approval: tenant, caller, delegator, tool, arguments, state, approver | 3600 s, deleted when used |
| `scout:approvals:<tenant>` | sorted set of that tenant's request ids, scored by expiry time (the inbox) | 3600 s; entries pruned on read and removed when decided |
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
