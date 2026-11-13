# Data model

Every store, what is in it, and how tenancy is enforced there. Seed data is in `deploy/sql/init.sql` (Postgres) and `deploy/seed/seed.py` (Elasticsearch and Qdrant). Two demo tenants: `acme` and `globex`.

## Postgres

Tables: `customers`, `orders`, `documents`. All have `tenant_id TEXT NOT NULL`.

| Table | Key columns |
|-------|-------------|
| `customers` | `id` (e.g. `CUST-1001`), `name`, `email`, `tier`, `created_at` |
| `orders` | `id` (e.g. `o_9002`), `customer_id` -> customers, `status`, `total_cents`, `currency`, `created_at` |
| `documents` | `id` (e.g. `doc_refund_policy`), `title`, `body`, `url`, `created_at` |

Isolation:

- RLS is enabled and **forced** on all three. Policy: `tenant_id = current_setting('app.tenant_id', true)`. If the setting is missing the comparison is NULL and no rows come back.
- Two roles. `atlas` (the compose superuser) creates the schema. `atlas_app` has `SELECT` on the three tables, `NOSUPERUSER NOBYPASSRLS`. The server must connect as `atlas_app`: superusers skip RLS entirely.
- `postgres.query` runs in a read-only transaction and calls `set_config('app.tenant_id', $1, true)` first. The `true` makes it transaction-local.

## Elasticsearch

| Index | Fields | Notes |
|-------|--------|-------|
| `tickets` | `customer_id`, `status`, `priority`, `subject`, `created_at`, `_tenant` | `status` is `open` or `pending` in the seed data |
| `documents` | `title`, `body`, `_tenant` | mirror of the Postgres `documents` rows, `_id` is the document id |

The tool wraps every query: `bool.must = [caller query]`, `bool.filter = [term _tenant]`.

## Qdrant

One collection, `support_docs`: cosine distance, 768 dimensions (bge-base-en-v1.5). Point ids are UUIDs derived from the document id, because Qdrant only accepts integers or UUIDs. Payload:

| Field | Meaning |
|-------|---------|
| `doc_id` | The Postgres and Elasticsearch document id. Composed tools join on this. |
| `tenant` | Pinned by a mandatory `must` clause on every search. |
| `title`, `text` | For previews without a database round trip. |

Changing the embedding model changes the dimension. Re-run the seed script and re-embed any real data, or searches return noise.

## Redis

| Key | Holds | TTL |
|-----|-------|-----|
| `atlas:<tool>:<24 hex>` | cached tool result, hash of tool + tenant + arguments | per tool, L2 default 600 s |
| `atlas:rl:<tenant>:<tool>` | token bucket: `tokens`, `last_ms` | 3600 s |
| `atlas:approval:<id>` | pending approval: tenant, caller, delegator, tool, arguments, state, approver | 3600 s |
| `<cache key>:lock` | stampede lock | 5 s |

## S3 (MinIO in compose)

Bucket `atlas-mcp-data`. Object key is `<tenant>/<key>`. A tenant cannot name another tenant's prefix because the server adds its own.

## Audit log

Newline-delimited JSON at `ATLAS_AUDIT_LOG_PATH`, one line per tool call, also printed to stdout. Fields: `ts`, `trace_id`, `tenant`, `caller`, `delegator`, `tool`, `args_hash`, `duration_ms`, `status`, `error_code`. Arguments are hashed with SHA-256 and never written, because they can hold personal data.

## Metrics

| Name | Labels |
|------|--------|
| `atlas_tool_calls_total` | `tool`, `status` (`ok` or an error code) |
| `atlas_tool_latency_seconds` | `tool` |
| `atlas_cache_hits_total`, `atlas_cache_misses_total` | `tool` |
| `atlas_rate_limited_total` | `tool` |

No tenant or caller labels: they multiply the series count. Unregistered tool names are recorded as `unknown`.
