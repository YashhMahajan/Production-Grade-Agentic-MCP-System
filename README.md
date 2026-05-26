<div align="center">

<br/>

<img src="https://miro.medium.com/v2/resize:fit:4800/1*vPJ1Xag-f3cgOgSA4QTeXQ.png" alt="Atlas MCP — Production-Grade MCP Server" width="100%"/>

<br/><br/>

# Atlas MCP

### Production-Grade Model Context Protocol Server + Agentic System

*The reference implementation that actually ships — not another hello-world demo.*

<br/>

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![MCP 2026](https://img.shields.io/badge/MCP-2026-6B46C1?style=for-the-badge)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-22C55E?style=for-the-badge)](./LICENSE)
[![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-Enabled-F5A623?style=for-the-badge)](https://opentelemetry.io/)
[![Prometheus](https://img.shields.io/badge/Prometheus-Metrics-E6522C?style=for-the-badge&logo=prometheus&logoColor=white)](https://prometheus.io/)

<br/>

> **Multi-tenant · OAuth 2.1 · Rate-limited · Circuit-broken · Cached · Observable · Policy-governed**

</div>

<br/>

## 🎯 The Problem This Solves

Every MCP tutorial ends at the `@tool` decorator returning `"hello world"`.

That's fine for a prototype. It's not what runs at 3 AM when your auth service goes down, a tenant leaks into another's data, the vector DB is timing out, and your agent is hammering the rate limit.

**Atlas MCP is the bridge between "it works locally" and "it runs in production."** It exposes a company's full heterogeneous data layer — Postgres, Elasticsearch, S3, vector DB — to AI agents as a single, secure, observable tool surface. Twelve production concerns are solved up front, each in its own module, each independently replaceable.

It ships with a **four-agent support copilot** (Planner → Retriever → Synthesizer → Critic) that exercises every component end to end, so you can see the whole system working before you swap in your own domain logic.

<br/>

---

## 🏗️ Architecture

<div align="center">

<img src="images/41f91cd1-dd50-4b32-a8d9-ad0c053fd0cc.png" width="90%"/>

<sub>The complete system: MCP dispatch pipeline (right) · Four-agent orchestrator (left) · Data plane (top) · Observability (bottom) · Identity and governance as crosscutting concerns</sub>

</div>

<br/>

---

## 🧩 Twelve Production Components

> These are the twelve things that keep showing up on the pager when teams skip them. Each one lives in its own module, fully decoupled.

<br/>

<table>
<thead>
<tr>
<th align="center">№</th>
<th>Component</th>
<th>Module</th>
<th>What It Gives You</th>
</tr>
</thead>
<tbody>
<tr>
<td align="center"><b>01</b></td>
<td>🚪 <b>Transport & Session Layer</b></td>
<td><code>server.py</code></td>
<td>stdio for local, Streamable HTTP for remote; horizontally scalable sessions with full middleware chain</td>
</tr>
<tr>
<td align="center"><b>02</b></td>
<td>🔐 <b>Authentication Server</b></td>
<td><code>auth/oauth.py</code></td>
<td>OAuth 2.1 + PKCE flow, short-lived JWTs, JWKS endpoint validation — drop in your own provider</td>
</tr>
<tr>
<td align="center"><b>03</b></td>
<td>⚖️ <b>Authorization & Policy Engine</b></td>
<td><code>auth/policy.py</code></td>
<td>Tool-level RBAC, tenant-scoped ABAC, YAML-driven rules, deny-by-default posture</td>
</tr>
<tr>
<td align="center"><b>04</b></td>
<td>📚 <b>Tool Registry & Discovery</b></td>
<td><code>tools/registry.py</code></td>
<td>Dynamic toolset registration, <code>.well-known</code> capability metadata endpoint, runtime hot-swap</td>
</tr>
<tr>
<td align="center"><b>05</b></td>
<td>✅ <b>Input Validation Layer</b></td>
<td><code>validation/schemas.py</code></td>
<td>Pydantic v2 schemas, strict enum constraints — agent-adversarial input is the default threat model</td>
</tr>
<tr>
<td align="center"><b>06</b></td>
<td>🔧 <b>Tool Execution Engine</b></td>
<td><code>tools/base.py</code></td>
<td>Three-level hierarchy: Atomic (one backend) → Composed (deterministic chains) → Workflow (multi-step)</td>
</tr>
<tr>
<td align="center"><b>07</b></td>
<td>🔄 <b>Circuit Breaker & Retry</b></td>
<td><code>reliability/</code></td>
<td>Closed → Open → Half-open state machine per tool; Adaptive Timeout Budget Allocation (ATBA)</td>
</tr>
<tr>
<td align="center"><b>08</b></td>
<td>🚦 <b>Rate Limiting & Quotas</b></td>
<td><code>ratelimit/limiter.py</code></td>
<td>Redis token-bucket (Lua-atomic), per-tenant <em>and</em> per-tool granularity, burst headroom config</td>
</tr>
<tr>
<td align="center"><b>09</b></td>
<td>⚡ <b>Caching Layer</b></td>
<td><code>cache/manager.py</code></td>
<td>Two-tier cache (L1 in-process LRU, L2 Redis), thundering-herd / stampede prevention</td>
</tr>
<tr>
<td align="center"><b>10</b></td>
<td>🧱 <b>Structured Error Framework</b></td>
<td><code>errors/framework.py</code></td>
<td>Machine-readable SERF errors with <code>retryable</code>, <code>hint</code>, and MCP-level payload fields</td>
</tr>
<tr>
<td align="center"><b>11</b></td>
<td>🔭 <b>Observability Stack</b></td>
<td><code>observability/</code></td>
<td>OpenTelemetry distributed traces, Prometheus metrics, structured JSONL audit log — one trace ID end-to-end</td>
</tr>
<tr>
<td align="center"><b>12</b></td>
<td>🛡️ <b>Governance & Multi-Tenancy</b></td>
<td><code>governance/</code></td>
<td>Hard tenant isolation, human-in-the-loop approval gates, per-tenant outbound HTTP allowlisting</td>
</tr>
</tbody>
</table>

<br/>

---

## 📐 Deep Dives, Section by Section

<table>
<tr>
<td width="50%" align="center">

### 📦 Data Persistence Layer
<img src="https://miro.medium.com/v2/resize:fit:4800/1*kT_lhnF50R4aM2iXXahMoA.png" width="100%"/>

**PostgreSQL 16 + Row-Level Security**
Tenant isolation enforced at the database layer. No application-level filtering that can be forgotten.

</td>
<td width="50%" align="center">

### 🚪 Transport & Session Layer
<img src="https://miro.medium.com/v2/resize:fit:4800/1*7GEV6AlegLbxX-dqJXHUdA.png" width="100%"/>

**Dual Transport · Stateless Sessions**
stdio for local tooling, Streamable HTTP for production clusters. Middleware chain is composable.

</td>
</tr>
<tr>
<td width="50%" align="center">

### 🔐 Auth, Policy & Governance
<img src="https://miro.medium.com/v2/resize:fit:4800/1*m45EPmIT1_5EmKNR4EEpLQ.png" width="100%"/>

**OAuth 2.1 · YAML Policies · Approval Gates**
Pluggable identity providers. Policy rules are YAML — readable by humans, enforced by the engine.

</td>
<td width="50%" align="center">

### 🔧 Tool Execution Engine
<img src="https://miro.medium.com/v2/resize:fit:4800/1*ak49o0j_5qLbvvM-zkkF_A.png" width="100%"/>

**Atomic → Composed → Workflow**
A clear three-level hierarchy prevents tools from becoming unmaintainable monoliths.

</td>
</tr>
<tr>
<td width="50%" align="center">

### 🔄 Reliability Layer
<img src="https://miro.medium.com/v2/resize:fit:4800/1*rjIJxzUpMhJ9BGffTczvLA.png" width="100%"/>

**Circuit Breakers · Retry · ATBA**
Cascading failures are contained. Timeouts are allocated adaptively based on remaining budget.

</td>
<td width="50%" align="center">

### ⚡ Rate Limiting & Caching
<img src="https://miro.medium.com/v2/resize:fit:4800/1*CvfLYyppMTLyU9UalfHmyA.png" width="100%"/>

**Redis Token Bucket · Two-tier Cache**
Atomicity guaranteed via Lua scripts. Stampede locks prevent cache-miss avalanches under load.

</td>
</tr>
<tr>
<td width="50%" align="center">

### 🔭 Observability Stack
<img src="https://miro.medium.com/v2/resize:fit:4800/1*dMi7KXpUfoMMsFpVTS8Acg.png" width="100%"/>

**OTel Traces · Prometheus · Audit Logs**
A single trace ID threads through every component — from the agent call to the DB query to the cache hit.

</td>
<td width="50%" align="center">

### 🤖 Multi-Agent Architecture
<img src="https://miro.medium.com/v2/resize:fit:4800/1*rasNhRMj5Ei93-AEQrbBwQ.png" width="100%"/>

**Planner · Retriever · Synthesizer · Critic**
Responsibilities are separated cleanly. The Critic enforces quality with a single bounded revise loop.

</td>
</tr>
</table>

<div align="center">

### 🎼 End-to-End Orchestrator Flow

<img src="https://miro.medium.com/v2/resize:fit:4800/1*7wyopmnCF_mEdxnI8u02uA.png" width="80%"/>

<sub>The four-agent pipeline from raw query → retrieval plan → tool execution → draft → critique → final answer</sub>

</div>

<br/>

---

## 🚀 Quick Start

### Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Docker + Docker Compose | Latest | Required for the full stack |
| Python | 3.11+ | Only needed to run the CLI locally |
| OpenRouter API key | — | Powers the agent layer |

<br/>

### Step 1 — Clone and configure

```bash
git clone https://github.com/FareedKhan-dev/production-grade-mcp-agentic-system.git
cd production-grade-mcp-agentic-system
cp .env.example .env
```

Open `.env` and set at minimum:

```bash
OPENROUTER_API_KEY=sk-or-v1-...          # Agent LLM calls
ATLAS_AUTH_JWKS_URL=https://...          # Your OAuth 2.1 JWKS endpoint
                                         # (leave default for local dev)
```

<br/>

### Step 2 — Bring up the full stack

```bash
docker compose up -d
```

The following services start immediately:

| Service | URL | Purpose |
|---------|-----|---------|
| 🏛️ **MCP Server** | `http://localhost:8080/mcp` | Streamable HTTP MCP endpoint |
| 🔍 **Discovery** | `http://localhost:8080/.well-known/mcp-server` | Unauthenticated capability metadata |
| 📊 **Metrics** | `http://localhost:8080/metrics` | Prometheus scrape target |
| ❤️ **Health** | `http://localhost:8080/healthz` | Liveness / readiness probe |
| 🔭 **Jaeger UI** | `http://localhost:16686` | Distributed trace explorer |
| 📈 **Grafana** | `http://localhost:3000` | Pre-built metrics dashboards *(admin / admin)* |
| 🗄️ **MinIO Console** | `http://localhost:9001` | S3-compatible object storage UI |

<br/>

### Step 3 — Run the support copilot CLI

```bash
pip install -e .

export ATLAS_MCP_URL=http://localhost:8080
export ATLAS_MCP_TOKEN=dev-token
export ATLAS_TENANT=acme
export OPENROUTER_API_KEY=sk-or-v1-...

atlas-copilot "Why was the refund on order o_9002 for CUST-1001 delayed?"
```

**What you'll see:**
- All four agents execute in sequence with real-time output
- The final answer is printed with `[S1]` `[S2]` inline citations
- A run summary shows token counts, tool calls, and latency
- The `run_id` ties back to a full distributed trace in Jaeger

<br/>

### Step 4 — Connect from Claude Desktop or Cursor

Add this block to your MCP host configuration:

```json
{
  "mcpServers": {
    "atlas-mcp": {
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

<br/>

---

## 📂 Repository Structure

```
atlas-mcp/
│
├── 🐳 docker-compose.yml          # Full local stack: app + data + observability
├── 🐳 Dockerfile                  # Two-stage build, non-root runtime user
├── 📦 pyproject.toml              # Dependencies, dev tooling, CLI entry points
├── ⚙️  .env.example                # Every setting documented, per component
│
├── config/
│   ├── http_allowlist.yaml        # Per-tenant outbound HTTP allowlist
│   └── policy.yaml                # YAML-driven authorization policy rules
│
├── deploy/
│   ├── otel/config.yaml           # OpenTelemetry Collector pipeline config
│   ├── prometheus/prometheus.yml  # Prometheus scrape target definitions
│   └── sql/init.sql               # Schema, RLS policies, seed data
│
├── docs/
│   ├── ARCHITECTURE.md            # The 12 components in depth
│   ├── AGENT_SYSTEM.md            # Multi-agent orchestrator internals
│   └── DEPLOYMENT.md              # Kubernetes, Cloudflare Workers, bare-metal
    └── startup_system.ipynb       # starting the system
│
└── src/atlas_mcp/
    │
    ├── server.py                  # ⚡ Component 1 — Transport & dispatch
    ├── config.py                  # Centralized, typed settings (Pydantic Settings)
    │
    ├── agents/                    # ── Four-agent support copilot ──────────────
    │   ├── planner.py             # Emits structured retrieval plan JSON
    │   ├── retriever.py           # Bounded tool-calling loop with budget
    │   ├── synthesizer.py         # Drafts reply with inline citations
    │   ├── critic.py              # Approves or issues a single revise signal
    │   ├── orchestrator.py        # Wires the four agents together
    │   ├── mcp_client.py          # Thin JSON-RPC client for the MCP server
    │   ├── memory.py              # STM (Redis) + LTM (Qdrant vector)
    │   └── cli.py                 # atlas-copilot CLI entry point
    │
    ├── auth/                      # ── Components 2 + 3 ────────────────────────
    │   ├── oauth.py               # JWT decoding + JWKS validation
    │   ├── middleware.py          # Bearer token extraction from headers
    │   └── policy.py              # YAML-driven RBAC / ABAC policy engine
    │
    ├── governance/                # ── Component 12 ────────────────────────────
    │   ├── tenant.py              # Tenant pinning + isolation middleware
    │   └── approval.py            # Human-in-the-loop approval gate
    │
    ├── tools/                     # ── Components 4 + 6 ────────────────────────
    │   ├── registry.py            # In-memory index + .well-known endpoint
    │   ├── base.py                # Abstract tool base class + metadata schema
    │   ├── atomic/                # Level 1 — one backend call per tool
    │   ├── composed/              # Level 2 — deterministic multi-step chains
    │   └── workflow/              # Level 3 — full multi-step procedures
    │
    ├── reliability/               # ── Component 7 ─────────────────────────────
    │   ├── circuit_breaker.py     # 3-state machine (closed / open / half-open)
    │   ├── retry.py               # Exponential backoff with full jitter
    │   └── atba.py                # Adaptive Timeout Budget Allocation
    │
    ├── ratelimit/                 # ── Component 8 ─────────────────────────────
    │   └── limiter.py             # Redis token bucket, Lua-atomic operations
    │
    ├── cache/                     # ── Component 9 ─────────────────────────────
    │   └── manager.py             # L1 (in-process LRU) + L2 (Redis) + stampede lock
    │
    ├── errors/                    # ── Component 10 ────────────────────────────
    │   └── framework.py           # SERF: Structured Error Recovery Framework
    │
    ├── observability/             # ── Component 11 ────────────────────────────
    │   ├── tracing.py             # OpenTelemetry span instrumentation
    │   ├── metrics.py             # Prometheus counter / histogram / gauge setup
    │   └── audit.py               # Structured JSONL append-only audit log
    │
    └── validation/                # ── Component 5 ─────────────────────────────
        └── schemas.py             # Tool call envelope schema (Pydantic v2)
```

<br/>

---

## 🎨 Technology Stack

<table>
<thead>
<tr><th>Layer</th><th>Technology</th><th>Why This Choice</th></tr>
</thead>
<tbody>
<tr><td><b>Language</b></td><td>Python 3.11+</td><td>Async-first, ecosystem fit, asyncio improvements in 3.11</td></tr>
<tr><td><b>Web Framework</b></td><td>Starlette + Uvicorn</td><td>Lightweight ASGI, middleware composability, no ORM opinions</td></tr>
<tr><td><b>MCP SDK</b></td><td><code>mcp &gt;= 1.2.0</code></td><td>Official SDK, Streamable HTTP transport support</td></tr>
<tr><td><b>Auth</b></td><td>PyJWT + Authlib</td><td>OAuth 2.1 resource server with JWKS validation and PKCE support</td></tr>
<tr><td><b>Validation</b></td><td>Pydantic v2 + Pydantic Settings</td><td>Strict type enforcement, schema generation, settings management</td></tr>
<tr><td><b>Database</b></td><td>asyncpg + PostgreSQL 16</td><td>Row-Level Security, async driver, native JSON ops</td></tr>
<tr><td><b>Search</b></td><td>Elasticsearch 8 (async)</td><td>Full-text + vector hybrid search in a single index</td></tr>
<tr><td><b>Vector DB</b></td><td>Qdrant</td><td>High-performance ANN, payload filtering, tenant namespacing</td></tr>
<tr><td><b>Object Storage</b></td><td>aioboto3 (MinIO / S3)</td><td>S3-compatible; swap MinIO for AWS S3 with zero code changes</td></tr>
<tr><td><b>Cache + State</b></td><td>Redis 7 + hiredis</td><td>Token buckets, L2 cache, STM memory, stampede locks</td></tr>
<tr><td><b>Retry / Reliability</b></td><td>tenacity + custom ATBA</td><td>Declarative retry policy; custom budget allocator for timeout governance</td></tr>
<tr><td><b>Tracing</b></td><td>OpenTelemetry SDK + OTLP</td><td>Vendor-neutral; export to Jaeger locally, any backend in production</td></tr>
<tr><td><b>Metrics</b></td><td>prometheus_client</td><td>Industry standard; Grafana dashboards included</td></tr>
<tr><td><b>Logging</b></td><td>structlog (JSON)</td><td>Structured output, zero-cost log levels, SIEM-friendly</td></tr>
<tr><td><b>LLM</b></td><td>OpenRouter → DeepSeek</td><td>Model-agnostic routing; swap model string to change providers</td></tr>
</tbody>
</table>

<br/>

---

## 🧪 Testing

The test suite is intentionally narrow — it covers the **three load-bearing safety properties** that must never regress:

```bash
pip install -e ".[dev]"
pytest -v
```

| Test File | What It Proves |
|-----------|----------------|
| `test_circuit_breaker.py` | State machine transitions are correct; retryable vs. deterministic error classification is accurate |
| `test_errors.py` | SERF wire format is stable; retry semantics match the error classification |
| `test_policy.py` | Default-deny posture holds; deny-beats-allow; glob matching; PII condition blocking |

> **Philosophy:** Wide unit-test coverage of pure business logic is welcome. What cannot be left untested is anything that could accidentally open a security or availability hole. These three suites are the non-negotiable floor.

<br/>

---

## 🛣️ Production Deployment

For deploying to a real environment — managed Postgres, production OAuth, Kubernetes, SIEM integration — see [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md).

The design is built for the swap:

| Local (docker-compose) | Production Equivalent |
|------------------------|----------------------|
| Dev JWT issuer | WorkOS AuthKit · Auth0 · Keycloak |
| MinIO container | AWS S3 · Google Cloud Storage · Azure Blob |
| PostgreSQL container | AWS RDS · Cloud SQL · Supabase |
| Redis container | Upstash · ElastiCache · MemoryDB |
| Local OTel collector | Datadog · Honeycomb · Grafana Cloud |
| File-based audit log | Splunk · Google Chronicle · any SIEM |

Every swap is a config change. No code changes required.

<br/>

---

## 📚 Documentation Index

| Resource | Description |
|----------|-------------|
| 🏗️ **[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)** | The twelve components in full engineering depth |
| 🤖 **[`docs/AGENT_SYSTEM.md`](./docs/AGENT_SYSTEM.md)** | Multi-agent orchestrator design, memory model, and bounded loop mechanics |
| 🚢 **[`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md)** | Kubernetes manifests, Cloudflare Workers notes, bare-metal considerations |

<br/>

---

## 📜 License

MIT — see [`LICENSE`](./LICENSE).

<br/>

---

<div align="center">

<br/>

**Built with ☕ and with an unforgiving spririt xD**

If this helped you, or want to connect then do ping me on [Linkedin](https://www.linkedin.com/in/yashh-mahajan-4ba51329a/)

<br/>

</div>
