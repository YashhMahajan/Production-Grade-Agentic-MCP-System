"""End-to-end tests against the running compose stack.

Skipped unless SCOUT_E2E=1. CI starts the stack, seeds it, and sets that variable.
Locally: `docker compose up -d --wait`, seed, then `SCOUT_E2E=1 pytest tests/e2e`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import httpx
import pytest

from scout_mcp.agents.mcp_client import ScoutMCPClient, fetch_token

MCP_URL = os.environ.get("SCOUT_E2E_MCP_URL", "https://localhost:8443")
TOKEN_URL = os.environ.get(
    "SCOUT_E2E_TOKEN_URL", "http://localhost:8081/realms/scout/protocol/openid-connect/token")
SECRETS = {  # dev secrets from deploy/keycloak/scout-realm.json
    "copilot-acme": "dev-secret-copilot-acme",
    "copilot-globex": "dev-secret-copilot-globex",
    "writer-acme": "dev-secret-writer-acme",
    "reviewer-acme": "dev-secret-reviewer-acme",
}
TENANT_OF = {"copilot-acme": "acme", "copilot-globex": "globex",
             "writer-acme": "acme", "reviewer-acme": "acme"}

pytestmark = pytest.mark.skipif(os.environ.get("SCOUT_E2E") != "1", reason="set SCOUT_E2E=1")


def pytest_collection_modifyitems(items):
    for item in items:
        if "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.skipif(os.environ.get("SCOUT_E2E") != "1",
                                               reason="set SCOUT_E2E=1"))


async def token(client_id: str) -> str:
    return await fetch_token(TOKEN_URL, client_id, SECRETS[client_id])


def mcp(client_id: str, bearer: str, tenant: str | None = None) -> ScoutMCPClient:
    # The edge proxy uses a local CA, so skip verification in the test run.
    return ScoutMCPClient(MCP_URL, bearer, tenant=tenant or TENANT_OF[client_id], verify=False)


@pytest.fixture
async def acme():
    async with mcp("copilot-acme", await token("copilot-acme")) as c:
        yield c


@pytest.fixture
async def globex():
    async with mcp("copilot-globex", await token("copilot-globex")) as c:
        yield c


@pytest.fixture
def http():
    with httpx.Client(verify=False, timeout=20) as c:
        yield c


def compose_exec(service: str, *cmd: str, stdin: str | None = None, env: dict | None = None) -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    args = ["docker", "compose", "exec", "-T"]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    out = subprocess.run([*args, service, *cmd], input=stdin, capture_output=True,
                         text=True, timeout=60)
    return out.stdout + out.stderr


def value(result):
    """Unwrap a ToolResult, failing with the server's own error message."""
    assert result.ok, f"{result.error_code}: {result.hint}"
    return result.value


__all__ = ["acme", "globex", "http", "compose_exec", "value", "token", "mcp", "json"]
