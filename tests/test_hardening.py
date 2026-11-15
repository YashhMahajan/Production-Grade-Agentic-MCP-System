"""Controls that stop development settings reaching production, and checks over every registered tool."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scout_mcp.config import ServerSettings
from scout_mcp.ratelimit.limiter import RateLimiter
from scout_mcp.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent

GOOD = dict(
    env="production",
    postgres_dsn="postgresql://app:x9@db:5432/scout",
    redis_url="redis://:s3cret@redis:6379/0",
    elasticsearch_password="es-real",
    vector_db_api_key="qd-real",
    auth_issuer="https://auth.example.com/realms/scout",
)


def settings(**over) -> ServerSettings:
    return ServerSettings(_env_file=None, **{**GOOD, **over})


def test_a_fully_configured_production_profile_has_no_problems():
    assert settings().production_problems() == []


@pytest.mark.parametrize("over, fragment", [
    ({"postgres_dsn": "postgresql://scout_app:scout_app@postgres:5432/scout"}, "postgres_dsn"),
    ({"redis_url": "redis://:scout-dev-redis@redis:6379/0"}, "redis_url"),
    ({"elasticsearch_password": "scout-dev-es"}, "elasticsearch_password"),
    ({"elasticsearch_password": None}, "elasticsearch_password"),
    ({"vector_db_api_key": "scout-dev-qdrant"}, "vector_db_api_key"),
    ({"auth_issuer": "http://keycloak:8080/realms/scout"}, "auth_issuer"),
    ({"policy_default_deny": False}, "policy_default_deny"),
    ({"require_tenant": False}, "require_tenant"),
    ({"destructive_tool_requires_approval": False}, "destructive_tool_requires_approval"),
])
def test_each_development_setting_is_reported(over, fragment):
    assert any(fragment in p for p in settings(**over).production_problems())


async def test_startup_refuses_development_settings_in_production():
    from scout_mcp.server import ScoutServer
    server = ScoutServer(settings(auth_issuer="http://keycloak:8080/realms/scout"))
    with pytest.raises(RuntimeError, match="auth_issuer"):
        await server.startup()


async def registered_tools():
    registry = ToolRegistry()
    await registry.discover()
    return list(registry)


async def test_every_tool_declares_scopes_that_some_policy_rule_grants():
    rules = yaml.safe_load((ROOT / "config/policy.yaml").read_text())["rules"]
    granted = {a for r in rules for a in r.get("actions", [])}
    for tool in await registered_tools():
        assert tool.meta.scopes_required, f"{tool.meta.name} has no scopes_required"
        for scope in tool.meta.scopes_required:
            assert scope in granted, f"{tool.meta.name}: no policy rule grants {scope}"


async def test_every_tool_that_writes_needs_approval():
    for tool in await registered_tools():
        if not tool.meta.read_only:
            assert tool.meta.destructive, f"{tool.meta.name} writes but needs no approval"


async def test_rate_limit_overrides_name_real_tools():
    names = {t.meta.name for t in await registered_tools()}
    limiter = RateLimiter(ServerSettings(_env_file=None))
    assert set(limiter._overrides) <= names


def test_coding_agent_entry_points_exist():
    assert (ROOT / "AGENTS.md").resolve() == (ROOT / "docs/AGENTS.md").resolve()
    text = (ROOT / "llms.txt").read_text()
    for line in text.splitlines():
        if "](" in line and line.startswith("- ["):
            target = line.split("](")[1].split(")")[0]
            assert (ROOT / target).exists(), f"llms.txt links to missing {target}"
