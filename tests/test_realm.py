"""What the Keycloak realm file must keep true, checked without a running Keycloak.

These are the properties a real sign-in depends on, each one learned from a failure or chosen
on purpose, so a change to the realm that breaks one fails here and not only in the stack job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REALM = json.loads((Path(__file__).resolve().parent.parent / "deploy/keycloak/scout-realm.json").read_text())
CLIENTS = {c["clientId"]: c for c in REALM["clients"]}
HUMAN = CLIENTS["scout-human"]
MAPPERS = {m["name"]: m for m in HUMAN["protocolMappers"]}


def test_the_sign_in_client_adds_the_subject_claim_itself():
    # Keycloak 26 takes `sub` from a built-in scope this realm does not define. Without the
    # mapper the token has no subject and the server refuses it ("missing the sub claim").
    assert MAPPERS["sub"]["protocolMapper"] == "oidc-sub-mapper"
    assert MAPPERS["sub"]["config"]["access.token.claim"] == "true"


def test_the_sign_in_client_maps_tenant_roles_and_audience_into_the_access_token():
    assert MAPPERS["tenant"]["protocolMapper"] == "oidc-usermodel-attribute-mapper"
    assert MAPPERS["tenant"]["config"]["user.attribute"] == "tenant"
    assert MAPPERS["roles"]["protocolMapper"] == "oidc-usermodel-realm-role-mapper"
    assert MAPPERS["roles"]["config"]["usermodel.realmRoleMapping.rolePrefix"] == "role:"
    assert MAPPERS["scout-audience"]["config"]["included.custom.audience"] == "scout-mcp"
    for name in ("tenant", "roles", "scout-audience"):
        assert MAPPERS[name]["config"]["access.token.claim"] == "true", name


def test_the_sign_in_client_is_public_pkce_only_and_has_no_secret():
    assert HUMAN["publicClient"] is True and "secret" not in HUMAN
    assert HUMAN["attributes"]["pkce.code.challenge.method"] == "S256"
    assert HUMAN["standardFlowEnabled"] is True
    assert not HUMAN["implicitFlowEnabled"] and not HUMAN["directAccessGrantsEnabled"]
    assert not HUMAN["serviceAccountsEnabled"] and HUMAN["fullScopeAllowed"] is False


def test_the_sign_in_client_redirects_only_to_exact_local_addresses():
    assert HUMAN["redirectUris"] and HUMAN["webOrigins"] == []
    for uri in HUMAN["redirectUris"]:
        assert "*" not in uri and uri.startswith(("http://localhost:", "http://127.0.0.1:")), uri


def test_a_person_can_carry_only_the_two_roles_the_server_knows():
    allowed = {r for m in REALM["scopeMappings"] if m["client"] == "scout-human" for r in m["roles"]}
    assert allowed == {"human_agent", "reviewer"}
    assert {r["name"] for r in REALM["roles"]["realm"]} == allowed


@pytest.mark.parametrize("user", REALM["users"], ids=lambda u: u["username"])
def test_every_user_has_a_tenant_and_exactly_one_role(user):
    assert user["attributes"]["tenant"] and len(user["attributes"]["tenant"]) == 1
    assert len(user["realmRoles"]) == 1 and user["realmRoles"][0] in {"human_agent", "reviewer"}
    assert user["credentials"][0]["temporary"] is False


def test_the_review_scope_and_role_exist_for_the_reviewer_identities():
    reviewer = CLIENTS["reviewer-acme"]
    roles = next(m for m in reviewer["protocolMappers"] if m["name"] == "roles")
    assert roles["config"]["claim.value"] == "role:reviewer"
    assert "approval:review" in reviewer["defaultClientScopes"]
