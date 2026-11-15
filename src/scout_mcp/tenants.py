"""``scout-mcp tenant add``: the Keycloak identities a new tenant needs.

A tenant gets three service identities, the same three the realm file defines for acme: a
copilot that reads, a writer that proposes uploads, and a reviewer that approves them. What goes
wrong when they are written by hand is the hard-coded ``tenant`` claim, because a typo there
reads another tenant's data. All three are built here from one definition, and a test holds
the result to the realm file's own clients field for field.

Secrets are generated, set on the client when it is created, and shown once.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import httpx

from scout_mcp.ingest import TENANT

# Names a person might pick that already mean something else.
RESERVED = frozenset({"default", "master", "scout"})

_CLAIM = {"access.token.claim": "true", "id.token.claim": "false", "userinfo.token.claim": "false"}

# kind -> what it is, the role its tokens carry, and the scopes it is given.
KINDS: dict[str, dict] = {
    "copilot": {"description": "Support copilot agent for tenant {tenant}", "role": "role:support_copilot",
                "scopes": ["tool:postgres:read", "tool:elasticsearch:read", "tool:vector:read", "tool:s3:read"]},
    "writer": {"description": "Agent that proposes attachment uploads (needs approval)", "role": "role:human_agent",
               "scopes": ["tool:s3:read", "tool:s3:write"]},
    "reviewer": {"description": "Human reviewer's service identity for approving pending calls",
                 "role": "role:reviewer", "scopes": ["approval:review"]},
}


class TenantError(Exception):
    pass


@dataclass
class Created:
    client_id: str
    secret: str


def check_tenant_name(name: str) -> None:
    if not TENANT.match(name) or name in RESERVED:
        raise TenantError(f"{name!r} is not a usable tenant name: lower case letters, digits, - and _, "
                          f"and not one of {', '.join(sorted(RESERVED))}")


def _hardcoded(name: str, claim: str, value: str) -> dict:
    return {"name": name, "protocol": "openid-connect", "protocolMapper": "oidc-hardcoded-claim-mapper",
            "consentRequired": False,
            "config": {**_CLAIM, "claim.name": claim, "claim.value": value, "jsonType.label": "String"}}


def client_representation(kind: str, tenant: str, secret: str) -> dict:
    """The Keycloak client for ``kind`` and ``tenant``, as the realm file writes it."""
    spec = KINDS[kind]
    client_id = f"{kind}-{tenant}"
    return {
        "clientId": client_id,
        "name": client_id,
        "description": spec["description"].format(tenant=tenant),
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "secret": secret,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "fullScopeAllowed": False,
        "defaultClientScopes": list(spec["scopes"]),
        "optionalClientScopes": [],
        "attributes": {"access.token.lifespan": "900"},
        "protocolMappers": [
            {"name": "scout-audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
             "consentRequired": False, "config": {**_CLAIM, "included.custom.audience": "scout-mcp"}},
            _hardcoded("tenant", "tenant", tenant),
            _hardcoded("roles", "roles", spec["role"]),
        ],
    }


async def add_tenant(
    tenant: str, *, url: str, realm: str, user: str, password: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Created]:
    """Create the tenant's clients. All or none: an existing client stops the run before any change,
    and a failure part way removes what was made."""
    check_tenant_name(tenant)
    clients = f"/admin/realms/{realm}/clients"
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=20, transport=transport) as http:
        signed_in = await http.post("/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli", "username": user, "password": password})
        if signed_in.status_code != 200:
            raise TenantError(f"could not sign in to Keycloak as admin user {user!r} (HTTP {signed_in.status_code})")
        headers = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}

        ids = [f"{kind}-{tenant}" for kind in KINDS]
        taken = []
        for client_id in ids:
            found = await http.get(clients, params={"clientId": client_id}, headers=headers)
            found.raise_for_status()
            if found.json():
                taken.append(client_id)
        if taken:
            raise TenantError(f"already exist in realm {realm}: {', '.join(taken)}. Nothing was changed.")

        made: list[tuple[Created, str]] = []
        try:
            for kind in KINDS:
                secret = secrets.token_urlsafe(32)
                rep = client_representation(kind, tenant, secret)
                response = await http.post(clients, json=rep, headers=headers)
                if response.status_code != 201:
                    raise TenantError(f"creating {rep['clientId']} failed (HTTP {response.status_code})")
                made.append((Created(rep["clientId"], secret), response.headers["Location"].rsplit("/", 1)[1]))
        except Exception as exc:
            for _, uuid in made:  # leave nothing half-made
                await http.delete(f"{clients}/{uuid}", headers=headers)
            if isinstance(exc, TenantError):
                raise
            raise TenantError(f"creating the clients failed: {exc}") from exc
        return [c for c, _ in made]
