"""End-to-end tests against the running compose stack.

Skipped unless SCOUT_E2E=1. CI starts the stack, seeds it, and sets that variable.
Locally: `docker compose up -d --wait`, seed, then `SCOUT_E2E=1 pytest tests/e2e`.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
from urllib.parse import parse_qs, urlparse

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

KEYCLOAK = os.environ.get("SCOUT_E2E_KEYCLOAK_URL", "http://localhost:8081")
HUMAN_REDIRECT = "http://localhost:8765/callback"  # registered on the scout-human client; nothing listens

pytestmark = pytest.mark.skipif(os.environ.get("SCOUT_E2E") != "1", reason="set SCOUT_E2E=1")


def pytest_collection_modifyitems(items):
    for item in items:
        if "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.skipif(os.environ.get("SCOUT_E2E") != "1",
                                               reason="set SCOUT_E2E=1"))


async def token(client_id: str) -> str:
    return await fetch_token(TOKEN_URL, client_id, SECRETS[client_id])


def claims_of(raw_token: str) -> dict:
    payload = raw_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _page_text(response: httpx.Response) -> str:
    """The readable text of a Keycloak page, so a failed sign-in says what Keycloak said."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", response.text, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())[:500]


def sign_in(username: str) -> str:
    """Sign a person in as the scout-human client does: authorization code with PKCE.

    Keycloak's pages carry the pinned host keycloak:8080, which only exists inside the
    network, so the form's address is pointed at the published port. Dev password.
    """
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    oidc = f"{KEYCLOAK}/realms/scout/protocol/openid-connect"
    with httpx.Client(follow_redirects=False, timeout=20) as client:
        page = client.get(f"{oidc}/auth", params={
            "client_id": "scout-human", "redirect_uri": HUMAN_REDIRECT, "response_type": "code",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "e2e"})
        assert page.status_code == 200, _page_text(page)
        form = re.search(r'<form[^>]*id="kc-form-login"[^>]*>(.*?)</form>', page.text, re.S)
        assert form, _page_text(page)
        action = html.unescape(re.search(r'action="([^"]+)"', form.group(0)).group(1))
        action = action.replace("http://keycloak:8080", KEYCLOAK)
        # What a browser would send: every hidden field the page carries, then the credentials.
        fields = {m.group(1): html.unescape(m.group(2)) for m in
                  re.finditer(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form.group(0))}
        # A browser sends the cookies Keycloak set on this page. Python's jar withholds some of them
        # from plain http://localhost (it applies Secure and domain rules a browser relaxes), which
        # Keycloak reports as "Cookie not found", so they are sent as given.
        cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
        done = client.post(action, headers={"Cookie": cookie},
                           data={**fields, "username": username, "password": f"dev-password-{username}"})
        attributes = [(c.name, c.domain, c.path, c.secure) for c in client.cookies.jar]
        assert done.status_code == 302, (f"{done.status_code}: {_page_text(done)}; cookies: {attributes}; "
                                         f"sent header: {done.request.headers.get('cookie', '')[:60]!r}")
        code = parse_qs(urlparse(done.headers["location"]).query)["code"][0]
        issued = client.post(f"{oidc}/token", data={
            "grant_type": "authorization_code", "client_id": "scout-human", "code": code,
            "code_verifier": verifier, "redirect_uri": HUMAN_REDIRECT})
        assert issued.status_code == 200, issued.text[:300]
        return issued.json()["access_token"]


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


__all__ = ["acme", "globex", "http", "compose_exec", "value", "token", "mcp", "json", "sign_in", "claims_of", "KEYCLOAK", "HUMAN_REDIRECT"]
