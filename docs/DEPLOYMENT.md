# Deployment
# how the project is going to work with differnt containers and envs 

## Local (docker compose)

```bash
cp .env.example .env
docker compose up -d
docker compose logs -f atlas-mcp
```

You get:

| Service | URL | Notes |
|---------|-----|-------|
| Atlas-MCP | `http://localhost:8080/mcp` | Streamable HTTP endpoint |
| Well-known metadata | `http://localhost:8080/.well-known/mcp-server` | Unauthenticated discovery |
| Metrics | `http://localhost:8080/metrics` | Prometheus |
| Health | `http://localhost:8080/healthz` | Liveness |
| Jaeger UI | `http://localhost:16686` | Traces |
| Grafana | `http://localhost:3000` | Metrics (admin/admin) |
| MinIO console | `http://localhost:9001` | S3 UI (minioadmin/minioadmin) |

## Connecting from Claude Desktop / Cursor

```json
{
  "mcpServers": {
    "atlas": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer ${ATLAS_MCP_TOKEN}",
        "X-Tenant-Id": "acme"
      }
    }
  }
}
```

## Running the support copilot

From the host (after `pip install -e .`):

```bash
export ATLAS_MCP_URL=http://localhost:8080
export ATLAS_MCP_TOKEN=dev-token
export ATLAS_TENANT=acme
export ATLAS_LLM_API_KEY=sk-or-v1-...

atlas-copilot "Why was the refund on order o_9002 for CUST-1001 delayed?"
```

## Production notes

- **Transport.** Put a TLS-terminating reverse proxy (Envoy, nginx, ALB)
  in front of Atlas. Stateless mode (the default) means any replica can
  serve any request.
- **Auth.** Replace the dev issuer with your real OAuth 2.1 provider.
  WorkOS AuthKit, Auth0, Descope, Keycloak all work — point
  `ATLAS_AUTH_JWKS_URL` at their JWKS endpoint.
- **Policy.** Ship `config/policy.yaml` as a ConfigMap / Secret. Rotate
  without redeploy by mounting it read-only.
- **Redis.** Use a managed Redis with persistence (ElastiCache, MemoryDB,
  Upstash). Atlas uses it for rate limits, cache L2, approvals, and agent
  STM; loss of Redis is loss of those features, not of the server.
- **Postgres.** RLS policies in `deploy/sql/init.sql` are the last line
  of tenant isolation. Keep them.
- **Observability.** Point `ATLAS_OTEL_ENDPOINT` at your collector and
  scrape `/metrics` from Prometheus. Every single tool call is traced.
- **Audit.** `/var/log/atlas/audit.jsonl` is append-only newline JSON.
  Ship it to your SIEM (Splunk, Datadog, Chronicle) and let the SIEM own
  retention.

## Scaling

- **Horizontal.** Stateless replicas behind a load balancer. Redis handles
  shared state.
- **Vertical knobs.** `ATLAS_CACHE_L1_MAX_ITEMS`, the Postgres pool size
  in `tools/atomic/postgres.py`, and the Uvicorn worker count in your
  process manager.
- **Cost.** The ATBA budget (`ATLAS_ATBA_TOTAL_BUDGET_MS`) caps per-turn
  latency. Combine with a small model for the planner/critic and a
  frontier model for the synthesizer — the Plan-and-Execute pattern can
  reduce cost by up to 90% versus running the frontier model for every
  step.
