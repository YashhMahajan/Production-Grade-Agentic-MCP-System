"""Central configuration for Scout-MCP.

Every subsystem pulls its settings from here so that the twelve components
have one shared source of truth, and so that nothing reads os.environ directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    """Top-level server configuration."""

    model_config = SettingsConfigDict(env_prefix="SCOUT_", env_file=".env", extra="ignore", frozen=True)

    # ── Component 1: Transport ──────────────────────────────────────────────
    transport: Literal["stdio", "http"] = "http"
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    # stateless_mode matters for horizontal scaling — see the 2026 MCP roadmap.
    stateless_mode: bool = True
    # Browser origins allowed to call /mcp. Empty means no browser origin is allowed;
    # clients that send no Origin header (agents, CLIs, SDKs) are never affected.
    allowed_origins: tuple[str, ...] = ()
    # The address clients use to reach this server, for the protected-resource
    # metadata. Unset means "work it out from the request".
    public_url: str | None = None
    # Tenant used when there is no token to read one from (stdio transport only).
    tenant: str = "default"

    # ── Component 2: Authentication ─────────────────────────────────────────
    auth_issuer: str = "https://auth.scout.local"
    auth_audience: str = "scout-mcp"
    auth_jwks_url: str = "https://auth.scout.local/.well-known/jwks.json"
    auth_signing_key_path: str | None = None
    auth_require_pkce: bool = True
    auth_access_token_ttl_seconds: int = 900  # 15 minutes

    # ── Component 3: Authorization ──────────────────────────────────────────
    policy_default_deny: bool = True
    policy_file: str = "config/policy.yaml"

    # ── Component 4: Tool registry ──────────────────────────────────────────
    dynamic_toolsets: bool = True
    well_known_metadata_path: str = "/.well-known/mcp-server"

    # ── Component 7: Reliability ────────────────────────────────────────────
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_seconds: int = 30
    retry_max_attempts: int = 3
    retry_base_delay_ms: int = 100
    # Adaptive Timeout Budget Allocation — total budget for a single agent request.
    atba_total_budget_ms: int = 30_000

    # ── Component 8: Rate limiting ──────────────────────────────────────────
    rate_limit_default_rpm: int = 60
    rate_limit_burst: int = 20
    # Tighter quotas for expensive tools (see ratelimit/limiter.py). Turn off to load test.
    rate_limit_per_tool_overrides: bool = True
    redis_url: str = "redis://localhost:6379/0"

    # ── Component 9: Caching ────────────────────────────────────────────────
    cache_l1_max_items: int = 10_000
    cache_l1_ttl_seconds: int = 60
    cache_l2_ttl_seconds: int = 600

    # ── Component 11: Observability ─────────────────────────────────────────
    otel_endpoint: str = "http://localhost:4317"
    service_name: str = "scout-mcp"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    metrics_enabled: bool = True
    audit_log_path: str = "/var/log/scout/audit.jsonl"

    # ── Component 12: Governance / multi-tenancy ───────────────────────────
    tenant_header: str = "X-Tenant-Id"
    require_tenant: bool = True
    destructive_tool_requires_approval: bool = True

    # ── Data layer ──────────────────────────────────────────────────────────
    postgres_dsn: str = "postgresql://scout:scout@localhost:5432/scout"
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_user: str = "elastic"
    elasticsearch_password: str | None = None  # unset means no authentication
    s3_endpoint: str | None = None
    s3_bucket: str = "scout-mcp-data"
    vector_db_url: str = "http://localhost:6333"  # Qdrant default
    vector_db_api_key: str | None = None

    # ── Embeddings ──────────────────────────────────────────────────────────
    # bge-base: 109M params / 768 dims. bge-large scores ~1 point higher on MTEB
    # retrieval (54.29 vs 53.25) for 3x the parameters and ~2.5x the latency.
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_cache_dir: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> ServerSettings:
    """Module-level singleton — read once, used everywhere."""
    return ServerSettings()
