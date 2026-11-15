"""`scout-mcp tenant add`: the three Keycloak identities a tenant needs, made the same way every time.

The thing that goes wrong by hand is the tenant claim: a client whose hard-coded `tenant` is a
typo reads another tenant's data. So the generated clients are held to the realm file's own
copilot, writer and reviewer for acme, field for field, and every one is checked for its claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scout_mcp.cli import main
from scout_mcp.tenants import KINDS, TenantError, add_tenant, client_representation

ROOT = Path(__file__).resolve().parent.parent
REALM = {c["clientId"]: c for c in json.loads((ROOT / "deploy/keycloak/scout-realm.json").read_text())["clients"]}


class Keycloak:
    """Just enough of the admin API: a token, a lookup by clientId, create and delete."""

    def __init__(self, existing=(), fail_on_create=None):
        self.clients: dict[str, dict] = {c: {"id": f"uuid-{c}"} for c in existing}
        self.fail_on_create, self.requests = fail_on_create, []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/realms/master/protocol/openid-connect/token":
            form = dict(p.split("=") for p in request.content.decode().split("&"))
            ok = form["username"] == "admin" and form["password"] == "pw" and form["client_id"] == "admin-cli"
            return httpx.Response(200 if ok else 401, json={"access_token": "admin-token"} if ok else {})
        if request.headers.get("authorization") != "Bearer admin-token":
            return httpx.Response(401)
        if path == "/admin/realms/scout/clients" and request.method == "GET":
            found = self.clients.get(request.url.params["clientId"])
            return httpx.Response(200, json=[found] if found else [])
        if path == "/admin/realms/scout/clients" and request.method == "POST":
            rep = json.loads(request.content)
            if rep["clientId"] == self.fail_on_create:
                return httpx.Response(500)
            self.clients[rep["clientId"]] = {"id": f"uuid-{rep['clientId']}", "rep": rep}
            return httpx.Response(201, headers={"Location": f"{request.url}/uuid-{rep['clientId']}"})
        if request.method == "DELETE" and path.startswith("/admin/realms/scout/clients/"):
            uuid = path.rsplit("/", 1)[1]
            self.clients = {k: v for k, v in self.clients.items() if v["id"] != uuid}
            return httpx.Response(204)
        return httpx.Response(404)

    def created(self) -> dict[str, dict]:
        return {k: v["rep"] for k, v in self.clients.items() if "rep" in v}

    def methods(self, method) -> list[str]:
        """Paths of the admin API calls made with this HTTP method (signing in is not counted)."""
        return [r.url.path for r in self.requests if r.method == method and r.url.path.startswith("/admin/")]


async def add(kc: Keycloak, tenant="initech", password="pw"):
    return await add_tenant(tenant, url="http://keycloak:8080", realm="scout", user="admin",
                            password=password, transport=kc.transport())


# ── The clients ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_generated_client_matches_the_realm_files_own_for_acme_field_for_field(kind):
    secret = REALM[f"{kind}-acme"]["secret"]
    assert client_representation(kind, "acme", secret) == REALM[f"{kind}-acme"]


async def test_a_new_tenant_gets_a_copilot_a_writer_and_a_reviewer(app_client=None):
    kc = Keycloak()
    created = await add(kc)
    assert [c.client_id for c in created] == ["copilot-initech", "writer-initech", "reviewer-initech"]
    assert set(kc.created()) == {c.client_id for c in created}


async def test_every_generated_client_carries_the_new_tenant_and_no_other():
    kc = Keycloak()
    await add(kc, tenant="initech")
    for client_id, rep in kc.created().items():
        claim = next(m for m in rep["protocolMappers"] if m["name"] == "tenant")
        assert claim["config"]["claim.value"] == "initech", client_id
        assert "acme" not in json.dumps(rep) and "globex" not in json.dumps(rep), client_id
        assert rep["publicClient"] is False and rep["standardFlowEnabled"] is False
        assert rep["directAccessGrantsEnabled"] is False and rep["fullScopeAllowed"] is False


async def test_each_secret_is_long_random_and_the_one_the_client_was_created_with():
    kc = Keycloak()
    created = await add(kc)
    secrets_ = [c.secret for c in created]
    assert len(set(secrets_)) == 3 and all(len(s) >= 32 for s in secrets_)
    assert not any(s.startswith("dev-secret") for s in secrets_)
    assert {c.client_id: c.secret for c in created} == {k: v["secret"] for k, v in kc.created().items()}


async def test_the_admin_api_is_called_with_the_admin_token_not_the_password():
    kc = Keycloak()
    await add(kc)
    api = [r for r in kc.requests if r.url.path.startswith("/admin/")]
    assert api and all(r.headers["authorization"] == "Bearer admin-token" for r in api)
    assert not any(b"pw" in r.content for r in api)


# ── What it refuses ────────────────────────────────────────────────────────
async def test_an_existing_client_stops_the_run_before_anything_is_created():
    kc = Keycloak(existing=["writer-initech"])
    with pytest.raises(TenantError, match="writer-initech"):
        await add(kc)
    assert kc.methods("POST") == [] and kc.methods("DELETE") == []


async def test_a_failure_part_way_removes_what_was_already_created():
    kc = Keycloak(fail_on_create="reviewer-initech")
    with pytest.raises(TenantError, match="reviewer-initech"):
        await add(kc)
    assert kc.created() == {}  # nothing half-made is left behind
    assert len(kc.methods("DELETE")) == 2


@pytest.mark.parametrize("name", ["Acme", "a b", "../x", "", "-x", "default", "master"])
async def test_a_name_that_is_not_a_usable_tenant_is_refused_before_any_request(name):
    kc = Keycloak()
    with pytest.raises(TenantError, match="tenant"):
        await add(kc, tenant=name)
    assert kc.requests == []


async def test_a_wrong_admin_password_is_an_error_that_names_no_secret():
    kc = Keycloak()
    with pytest.raises(TenantError, match="admin"):
        await add(kc, password="wrong")
    assert kc.methods("POST") == []


# ── The command ────────────────────────────────────────────────────────────
def test_the_command_prints_each_secret_once_and_the_next_steps(monkeypatch, capsys):
    from scout_mcp.tenants import Created

    async def fake(tenant, **kw):
        assert (tenant, kw["user"], kw["password"], kw["url"]) == ("initech", "admin", "pw", "http://kc:8080")
        return [Created("copilot-initech", "S1" * 20), Created("writer-initech", "S2" * 20), Created("reviewer-initech", "S3" * 20)]
    monkeypatch.setattr("scout_mcp.cli.add_tenant", fake)
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "pw")
    code = main(["tenant", "add", "initech", "--keycloak-url", "http://kc:8080", "--allow-host", "api.initech.test"])
    out = capsys.readouterr().out
    assert code == 0
    for secret in ("S1" * 20, "S2" * 20, "S3" * 20):
        assert out.count(secret) == 1
    assert "pw" not in out.split()  # the admin password is never echoed
    assert "scout-mcp ingest --tenant initech" in out
    assert "initech:\n  - api.initech.test" in out  # the allowlist snippet, to be added by a person
    assert "policy check" in out


def test_the_command_needs_the_admin_password_from_the_environment(monkeypatch, capsys):
    monkeypatch.delenv("KEYCLOAK_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr("scout_mcp.cli.add_tenant", lambda *a, **k: pytest.fail("reached Keycloak"))
    assert main(["tenant", "add", "initech"]) == 1
    assert "KEYCLOAK_ADMIN_PASSWORD" in capsys.readouterr().err


def test_the_command_reports_a_tenant_error_without_a_traceback(monkeypatch, capsys):
    async def refuse(tenant, **kw):
        raise TenantError("copilot-initech already exists")
    monkeypatch.setattr("scout_mcp.cli.add_tenant", refuse)
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "pw")
    assert main(["tenant", "add", "initech"]) == 1
    assert "already exists" in capsys.readouterr().err
