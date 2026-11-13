"""A small CLI so you can drive the copilot from the terminal.

Run with::

    atlas-copilot "Why was the refund on order o_9021 for CUST-1234 delayed?"

Needs ATLAS_MCP_URL, ATLAS_TENANT and ATLAS_LLM_API_KEY, plus either
ATLAS_MCP_TOKEN or ATLAS_TOKEN_URL with ATLAS_CLIENT_ID and ATLAS_CLIENT_SECRET
(the Keycloak client in deploy/keycloak). ATLAS_MCP_CA_BUNDLE points at the edge
proxy's CA certificate.
"""

#  this is to debug and run on local machine without docker
from __future__ import annotations

import asyncio
import json
import os
import sys

from atlas_mcp.agents.mcp_client import AtlasMCPClient, fetch_token
from atlas_mcp.agents.orchestrator import SupportCopilot


async def _main(question: str) -> int:
    url = os.environ.get("ATLAS_MCP_URL", "http://localhost:8080")
    tenant = os.environ.get("ATLAS_TENANT", "default")
    # Path to the edge proxy's CA certificate, or unset to use the system trust store.
    verify: bool | str = os.environ.get("ATLAS_MCP_CA_BUNDLE") or True
    token = os.environ.get("ATLAS_MCP_TOKEN")
    if not token:
        token_url = os.environ.get("ATLAS_TOKEN_URL")
        client_id = os.environ.get("ATLAS_CLIENT_ID")
        client_secret = os.environ.get("ATLAS_CLIENT_SECRET")
        if not (token_url and client_id and client_secret):
            print("error: set ATLAS_MCP_TOKEN, or ATLAS_TOKEN_URL with ATLAS_CLIENT_ID "
                  "and ATLAS_CLIENT_SECRET", file=sys.stderr)
            return 2
        token = await fetch_token(token_url, client_id, client_secret, verify=verify)

    async with AtlasMCPClient(url, token, tenant=tenant, verify=verify) as mcp:
        copilot = SupportCopilot(mcp)
        response = await copilot.answer(question)

    print("=" * 72)
    print("DRAFT REPLY (approved)" if response.approved else "DRAFT REPLY (not approved)")
    print("=" * 72)
    print(response.draft)
    print()
    print("=" * 72)
    print("TRACE SUMMARY")
    print("=" * 72)
    print(json.dumps(response.to_dict(), indent=2, default=str))
    return 0 if response.approved else 1


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: atlas-copilot \"<customer question>\"", file=sys.stderr)
        sys.exit(2)
    question = " ".join(sys.argv[1:])
    sys.exit(asyncio.run(_main(question)))


if __name__ == "__main__":
    main()
