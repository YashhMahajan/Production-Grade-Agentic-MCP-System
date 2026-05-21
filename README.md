<div align="center">

<img src="https://miro.medium.com/v2/resize:fit:4800/1*vPJ1Xag-f3cgOgSA4QTeXQ.png" alt="Production-Grade MCP Server + Agentic System" width="100%"/>

# 🏛️ Production-Grade MCP Server + Agentic System

### *A reference implementation of an MCP server designed to actually ship*

*Multi-tenant · Authenticated · Observable · Rate-limited · Cached · Circuit-broken · Governed*

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-c15f3c.svg?style=for-the-badge)](https://www.python.org/downloads/)
[![MCP 2026](https://img.shields.io/badge/MCP-2026-b1ada1.svg?style=for-the-badge)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-c15f3c.svg?style=for-the-badge)](./LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Compose-b1ada1.svg?style=for-the-badge&logo=docker)](https://docs.docker.com/compose/)

---

### 📖 Full Step-by-Step Blog Walkthrough

This repository is the companion codebase for a long-form blog post that walks through every single component end to end, with every line of code explained in context. **Start there if you want to understand the "why" behind the architecture before reading the code.**

### 🔗 [**Building a Production-Grade MCP Server Architecture with Agentic System →**](https://medium.com/@fareedkhandev/building-a-production-grade-mcp-server-architecture-with-agentic-system-de92127aca6f)

---

</div>

## 🎯 What This Is

Most MCP tutorials end with a `@tool` decorator that returns `"hello world"`. That is fine for a demo. It is not what ships.

This repository is a **reference implementation of an MCP server designed to run in production**: multi-tenant, authenticated, observable, rate-limited, cached, circuit-broken, and governed. It exposes a company's heterogeneous data layer (Postgres, Elasticsearch, S3, vector DB) to AI agents as a single, secure tool surface, and ships with a **four-agent support copilot** (Planner → Retriever → Synthesizer → Critic) that uses it end to end.

The codebase is deliberately organised around **twelve components** that keep showing up on the 3 AM pager when teams skip them. Each one lives in its own module and can be read, replaced, or extended independently.

---

## 🏗️ Architecture Overview

<div align="center">

<img src="https://miro.medium.com/v2/resize:fit:4800/1*vPJ1Xag-f3cgOgSA4QTeXQ.png" alt="Full Architecture" width="90%"/>

*The complete production-grade system: MCP server dispatch pipeline on the right, four-agent orchestrator on the left, data plane on top, observability on the bottom, identity and governance as crosscutting concerns.*

</div>

---

## 🧩 The 12 Components

| # | Component | Lives in | What it gives you |
|---|-----------|----------|-------------------|
| 1 | 🚪 **Transport & Session Layer** | `server.py` | stdio for local, Streamable HTTP for remote, horizontal-scale-friendly sessions |
| 2 | 🔐 **Authentication Server** | `auth/oauth.py` | OAuth 2.1 + PKCE, short-lived JWTs, JWKS validation |
| 3 | ⚖️ **Authorization & Policy Engine** | `auth/policy.py` | Tool-level RBAC, tenant-scoped ABAC, deny-by-default |
| 4 | 📚 **Tool Registry & Discovery** | `tools/registry.py` | Dynamic toolsets, `.well-known` capability metadata |
| 5 | ✅ **Input Validation Layer** | `validation/schemas.py` | Pydantic schemas, enum constraints, agent-adversarial input as default threat model |
| 6 | 🔧 **Tool Execution Engine** | `tools/base.py` | Three-level hierarchy (atomic / composed / workflow) |
| 7 | 🔄 **Circuit Breaker & Retry** | `reliability/` | Closed → open → half-open, Adaptive Timeout Budget Allocation |
| 8 | 🚦 **Rate Limiting & Quotas** | `ratelimit/limiter.py` | Redis token-bucket (Lua-atomic), per-tenant and per-tool |
| 9 | ⚡ **Caching Layer** | `cache/manager.py` | Two-tier (L1 in-process, L2 Redis), stampede prevention |
| 10 | 🧱 **Structured Error Framework** | `errors/framework.py` | Machine-readable errors with `retryable` and `hint` fields |
| 11 | 🔭 **Observability Stack** | `observability/` | OpenTelemetry traces, Prometheus metrics, audit logs |
| 12 | 🛡️ **Governance & Multi-Tenancy** | `governance/` | Tenant isolation, approval gates, outbound HTTP allowlisting |

---

## 📖 Diving Deeper, Section by Section

Each diagram below links back to the corresponding section in the blog, where every line of code is walked through in detail.

<table>
<tr>
<td width="50%" align="center">

### 📦 Data Persistence Layer
<img src="https://miro.medium.com/v2/resize:fit:4800/1*kT_lhnF50R4aM2iXXahMoA.png" alt="Data Persistence Layer" width="100%"/>

*Postgres + Row-Level Security · Tenant isolation at the DB layer*

</td>
<td width="50%" align="center">

### 🚪 Transport & Session Layer
<img src="https://miro.medium.com/v2/resize:fit:4800/1*7GEV6AlegLbxX-dqJXHUdA.png" alt="Transport Layer" width="100%"/>

*Dual transport · Stateless session · Middleware chain*

</td>
</tr>
<tr>
<td width="50%" align="center">

### 🔐 Authentication, Policy & Governance
<img src="https://miro.medium.com/v2/resize:fit:4800/1*m45EPmIT1_5EmKNR4EEpLQ.png" alt="Auth & Policy" width="100%"/>

*OAuth 2.1 · YAML policies · Human-in-the-loop approvals*

</td>
<td width="50%" align="center">

### 🔧 Tool Execution Engine
<img src="https://miro.medium.com/v2/resize:fit:4800/1*ak49o0j_5qLbvvM-zkkF_A.png" alt="Tool Execution" width="100%"/>

*Three-level hierarchy · Atomic · Composed · Workflow*

</td>
</tr>
<tr>
<td width="50%" align="center">

### 🔄 Reliability Layer
<img src="https://miro.medium.com/v2/resize:fit:4800/1*rjIJxzUpMhJ9BGffTczvLA.png" alt="Reliability" width="100%"/>

*Circuit breakers · Retry with jitter · ATBA budget allocator*

</td>
<td width="50%" align="center">

### ⚡ Rate Limiting & Caching
<img src="https://miro.medium.com/v2/resize:fit:4800/1*CvfLYyppMTLyU9UalfHmyA.png" alt="Rate Limit & Cache" width="100%"/>

*Redis token bucket · Two-tier cache · Stampede lock*

</td>
</tr>
<tr>
<td width="50%" align="center">

### 🔭 Observability Stack
<img src="https://miro.medium.com/v2/resize:fit:4800/1*dMi7KXpUfoMMsFpVTS8Acg.png" alt="Observability" width="100%"/>

*OpenTelemetry · Prometheus · Audit logs · One trace ID*

</td>
<td width="50%" align="center">

### 🤖 Multi-Agentic Architecture
<img src="https://miro.medium.com/v2/resize:fit:4800/1*rasNhRMj5Ei93-AEQrbBwQ.png" alt="Multi-Agent" width="100%"/>

*Four-agent design · Planner · Retriever · Synthesizer · Critic*

</td>
</tr>
</table>

<div align="center">

### 🎼 The Orchestrator Flow
<img src="https://miro.medium.com/v2/resize:fit:4800/1*7wyopmnCF_mEdxnI8u02uA.png" alt="Orchestrator" width="80%"/>

*End-to-end agent orchestration with one bounded revise loop*

</div>

---

## 🚀 Quick Start

### Prerequisites
- Docker & Docker Compose
- Python 3.11+ (only for running the CLI locally)
- An OpenRouter API key (for the agent layer)

### 1. Clone and Configure

```bash
git clone https://github.com/FareedKhan-dev/production-grade-mcp-agentic-system.git
cd production-grade-mcp-agentic-system
cp .env.example .env
```

Edit `.env` and set at minimum:
- `OPENROUTER_API_KEY` — for the agent layer
- `ATLAS_AUTH_JWKS_URL` — your OAuth 2.1 provider's JWKS endpoint (or leave default for dev)

### 2. Bring Up the Stack

```bash
docker compose up -d
```

That brings up the full local environment:

| Service | URL | What it is |
|---------|-----|------------|
| 🏛️ **MCP Server** | `http://localhost:8080/mcp` | Streamable HTTP endpoint |
| 🔍 **Discovery** | `http://localhost:8080/.well-known/mcp-server` | Unauthenticated capability metadata |
| 📊 **Metrics** | `http://localhost:8080/metrics` | Prometheus scrape target |
| ❤️ **Health** | `http://localhost:8080/healthz` | Liveness probe |
| 🔭 **Jaeger** | `http://localhost:16686` | Distributed tracing UI |
| 📈 **Grafana** | `http://localhost:3000` | Metrics dashboards *(admin / admin)* |
| 🗄️ **MinIO Console** | `http://localhost:9001` | S3-compatible storage UI |

### 3. Run the Support Copilot CLI

```bash
pip install -e .

export ATLAS_MCP_URL=http://localhost:8080
export ATLAS_MCP_TOKEN=dev-token
export ATLAS_TENANT=acme
export OPENROUTER_API_KEY=sk-or-v1-...

atlas-copilot "Why was the refund on order o_9002 for CUST-1001 delayed?"
```

You will see the four agents run end-to-end, the final draft printed with `[S1][S2]` citations, and a full trace summary including token counts, tool calls, and the run_id that ties back to Jaeger.

### 4. Connect from Claude Desktop / Cursor

Add this to your MCP host config:

```json
{
  "mcpServers": {
    "production-mcp": {
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

---

## 📂 Repository Structure

```
.
├── 📄 README.md
├── 🐳 docker-compose.yml          # Full local stack: app + data + observability
├── 🐳 Dockerfile                  # Two-stage build, non-root runtime
├── 📜 LICENSE
├── 📦 pyproject.toml              # Dependencies, dev tools, CLI entry points
├── ⚙️  .env.example                # Every setting documented by component
│
├── 🔧 config/                     # Runtime configuration (hot-reloadable)
│   ├── http_allowlist.yaml       # Per-tenant outbound HTTP allowlist
│   └── policy.yaml               # YAML-driven authorization policies
│
├── 🚢 deploy/                     # Deployment sidecar configs
│   ├── otel/config.yaml          # OpenTelemetry Collector pipeline
│   ├── prometheus/prometheus.yml # Prometheus scrape targets
│   └── sql/init.sql              # Schema + RLS policies + seed data
│
├── 📚 docs/                       # Deep-dive documentation
│   ├── AGENT_SYSTEM.md           # Multi-agent orchestrator internals
│   ├── ARCHITECTURE.md           # The 12 components in detail
│   └── DEPLOYMENT.md             # K8s, Cloudflare Workers, bare-metal
│
├── 🧠 src/atlas_mcp/              # Main application source
│   ├── config.py                 # Centralized typed settings
│   ├── server.py                 # ⚡ Component 1: Transport & dispatch
│   │
│   ├── 🤖 agents/                 # Four-agent support copilot
│   │   ├── planner.py            # Emits retrieval plan JSON
│   │   ├── retriever.py          # Bounded tool-calling loop
│   │   ├── synthesizer.py        # Drafts reply with citations
│   │   ├── critic.py             # Approves or sends one revise
│   │   ├── orchestrator.py       # Wires the four agents together
│   │   ├── mcp_client.py         # Thin JSON-RPC MCP client
│   │   ├── memory.py             # STM (Redis) + LTM (vector)
│   │   └── cli.py                # atlas-copilot CLI entry point
│   │
│   ├── 🔐 auth/                   # Components 2 + 3
│   │   ├── oauth.py              # JWT + JWKS validation
│   │   ├── middleware.py         # Bearer token extraction
│   │   └── policy.py             # YAML-driven policy engine
│   │
│   ├── 🛡️  governance/             # Component 12
│   │   ├── tenant.py             # Tenant pinning middleware
│   │   └── approval.py           # Human-in-the-loop gate
│   │
│   ├── 🔧 tools/                  # Components 4 + 6
│   │   ├── registry.py           # In-memory tool index + discovery
│   │   ├── base.py               # Tool abstract base + metadata
│   │   ├── atomic/               # Level 1: one backend each
│   │   ├── composed/             # Level 2: deterministic chains
│   │   └── workflow/             # Level 3: multi-step procedures
│   │
│   ├── 🔄 reliability/            # Component 7
│   │   ├── circuit_breaker.py    # 3-state machine per tool
│   │   ├── retry.py              # Exponential backoff + jitter
│   │   └── atba.py               # Adaptive Timeout Budget Allocation
│   │
│   ├── 🚦 ratelimit/              # Component 8
│   │   └── limiter.py            # Redis token bucket (Lua-atomic)
│   │
│   ├── ⚡ cache/                   # Component 9
│   │   └── manager.py            # L1 + L2 cache with stampede lock
│   │
│   ├── 🧱 errors/                 # Component 10
│   │   └── framework.py          # Structured Error Recovery (SERF)
│   │
│   ├── 🔭 observability/          # Component 11
│   │   ├── tracing.py            # OpenTelemetry spans
│   │   ├── metrics.py            # Prometheus instruments
│   │   └── audit.py              # Structured JSONL audit log
│   │
│   └── ✅ validation/             # Component 5
│       └── schemas.py            # Tool call envelope
│
└── 🧪 tests/                      # Narrow tests, load-bearing properties
    ├── test_circuit_breaker.py   # State machine transitions
    ├── test_errors.py            # SERF wire format + retry semantics
    └── test_policy.py            # Deny-beats-allow + default-deny
```

---

## 🎨 Tech Stack

| Layer | Technology |
|-------|-----------|
| **Language** | Python 3.11+ |
| **Web framework** | Starlette + Uvicorn |
| **MCP SDK** | `mcp>=1.2.0` |
| **Auth** | PyJWT + Authlib (OAuth 2.1 resource server) |
| **Validation** | Pydantic v2 + Pydantic Settings |
| **Database** | asyncpg (PostgreSQL 16 with RLS) |
| **Search** | Elasticsearch 8 (async client) |
| **Vector DB** | Qdrant |
| **Object storage** | aioboto3 (MinIO / S3) |
| **Cache + queues** | Redis 7 (`redis[hiredis]`) |
| **Reliability** | tenacity (retries) + custom breaker + custom ATBA |
| **Tracing** | OpenTelemetry SDK + OTLP exporter |
| **Metrics** | prometheus_client |
| **Logging** | structlog (JSON) |
| **LLM** | OpenRouter (DeepSeek) |

---

## 🧪 Testing

The test suite is deliberately narrow, covering the three load-bearing safety properties:

```bash
pip install -e ".[dev]"
pytest -v
```

- **`test_circuit_breaker.py`** — state machine transitions, retryable vs deterministic error classification
- **`test_errors.py`** — SERF wire format, retry semantics, MCP-level error data
- **`test_policy.py`** — default-deny, deny-beats-allow, glob matching, PII condition blocking

---

## 🛣️ Production Deployment

For running this in an actual production environment (managed Postgres, real OAuth provider, SIEM integration, Kubernetes), see [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md).

Key swaps between local dev and production:

| Local (docker-compose) | Production |
|------------------------|------------|
| Dev JWT issuer | WorkOS AuthKit / Auth0 / Keycloak |
| MinIO | AWS S3 / GCS / Azure Blob |
| Local Postgres | AWS RDS / Cloud SQL / Supabase |
| Redis container | Upstash / ElastiCache / MemoryDB |
| Local OTel collector | Datadog / Honeycomb / Grafana Cloud |
| File-based audit log | Splunk / Chronicle / SIEM of choice |

---

## 📚 Documentation

- 📖 [**Blog Walkthrough** — Building a Production-Grade MCP Server](https://medium.com/@fareedkhandev/building-a-production-grade-mcp-server-architecture-with-agentic-system-de92127aca6f) *(recommended starting point)*
- 🏗️ [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — The 12 components in depth
- 🤖 [`docs/AGENT_SYSTEM.md`](./docs/AGENT_SYSTEM.md) — Multi-agent orchestrator internals
- 🚢 [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md) — Production deployment options

---

## 📜 License

MIT. See [`LICENSE`](./LICENSE).

---

<div align="center">

### ⭐ If this helped you, please consider starring the repo

**Built with ☕ and a lot of 3 AM debugging**

📖 [Read the full blog walkthrough](https://medium.com/@fareedkhandev/building-a-production-grade-mcp-server-architecture-with-agentic-system-de92127aca6f) · 🐛 [Report an issue](../../issues) · 💬 [Start a discussion](../../discussions)

</div>