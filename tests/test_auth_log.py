"""A rejected token leaves a line saying why, and never the token."""

from __future__ import annotations

from tests.conftest import HEADERS

BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


async def test_a_rejected_token_is_logged_with_the_reason_but_never_the_token(app_client, caplog):
    with caplog.at_level("INFO", logger="scout_mcp.auth.middleware"):
        resp = await app_client.post("/mcp", json=BODY, headers={**HEADERS, "Authorization": "Bearer secret-looking-value"})
    assert resp.status_code == 401
    assert "token rejected: invalid_token" in caplog.text
    assert "secret-looking-value" not in caplog.text


async def test_a_missing_token_is_logged_too(app_client, caplog):
    with caplog.at_level("INFO", logger="scout_mcp.auth.middleware"):
        resp = await app_client.post("/mcp", json=BODY, headers=HEADERS)
    assert resp.status_code == 401 and "token rejected: missing_token" in caplog.text
