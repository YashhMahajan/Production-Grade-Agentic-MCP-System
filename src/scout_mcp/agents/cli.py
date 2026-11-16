"""A small CLI so you can drive the copilot from the terminal.

Run with::

    scout-copilot brief CUST-1234
    scout-copilot ask "Why was the refund on order o_9021 for CUST-1234 delayed?"
    scout-copilot "Why was the refund ..."          # a bare question means ask

``brief`` makes no model call and needs no LLM key. ``ask`` needs SCOUT_LLM_API_KEY.
When the model cannot be used (no key, the day's allowance spent, an error, a run that
hit its call limit) ``ask`` prints the customer brief instead and exits 3.

Both need SCOUT_MCP_URL and SCOUT_TENANT, plus either SCOUT_MCP_TOKEN or
SCOUT_TOKEN_URL with SCOUT_CLIENT_ID and SCOUT_CLIENT_SECRET (the Keycloak client in
deploy/keycloak). SCOUT_MCP_CA_BUNDLE points at the edge proxy's CA certificate.

Exit codes: 0 answered, 1 not approved or the lookup failed, 2 bad usage or settings,
3 evidence pack instead of a model answer.
"""

#  this is to debug and run on local machine without docker
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import httpx

from scout_mcp.agents.base import LLM
from scout_mcp.agents.brief import make_brief
from scout_mcp.agents.ledger import Ledger
from scout_mcp.agents.mcp_client import ScoutMCPClient, fetch_token
from scout_mcp.agents.orchestrator import SupportCopilot
from scout_mcp.agents.planner import PlannerAgent
from scout_mcp.agents.redact import RedactingLLM

EXIT_OK, EXIT_NOT_OK, EXIT_USAGE, EXIT_EVIDENCE = 0, 1, 2, 3


def free_model_problem(models: list[str]) -> str | None:
    """Free models send text to third-party hosts, so production needs an explicit opt-in."""
    if os.environ.get("SCOUT_ENV") != "production" or os.environ.get("SCOUT_ALLOW_FREE_LLM") == "1":
        return None
    free = [m for m in models if m.endswith(":free")]
    if free:
        return (f"refusing free model(s) {', '.join(free)} with SCOUT_ENV=production; "
                "set SCOUT_ALLOW_FREE_LLM=1 if sending customer text to those hosts is acceptable")
    return None


async def brief_command(mcp, customer_id: str, now: datetime) -> tuple[str, int]:
    """The customer brief from one tool call. No model."""
    result = await mcp.call_tool(
        "customer.build_context", {"customer_id": customer_id, "question": "brief"})
    if not result.ok:
        return f"could not load {customer_id}: {result.error_code}", EXIT_NOT_OK
    return make_brief(result.value, now).text, EXIT_OK


async def ask_command(copilot: SupportCopilot, mcp, question: str, ledger: Ledger,
                      now: datetime) -> tuple[str, int]:
    customer_id = PlannerAgent._extract_customer_id(question)

    async def evidence_pack(why: str) -> tuple[str, int]:
        if not customer_id:
            return f"{why}\nInclude a customer id (e.g. CUST-1234) to get a brief.", EXIT_EVIDENCE
        text, code = await brief_command(mcp, customer_id, now)
        return f"{why} Here is the evidence instead.\n\n{text}", EXIT_EVIDENCE if code == EXIT_OK else code

    allowed = ledger.remaining()
    if allowed < 1:
        return await evidence_pack("Today's model allowance is spent.")
    copilot.max_llm_calls = min(copilot.max_llm_calls, allowed)
    try:
        response = await copilot.answer(question)
    except (httpx.HTTPError, RuntimeError) as exc:
        return await evidence_pack(f"The model could not be used ({type(exc).__name__}).")
    ledger.record(response.llm_calls)

    if response.degraded:
        return await evidence_pack(f"The run stopped early ({response.degraded}).")
    header = "DRAFT REPLY (approved)" if response.approved else "DRAFT REPLY (not approved)"
    text = "\n".join([header, "=" * 72, response.draft, "", "TRACE SUMMARY", "=" * 72,
                      json.dumps(response.to_dict(), indent=2, default=str)])
    return text, EXIT_OK if response.approved else EXIT_NOT_OK


async def _main(command: str, argument: str) -> int:
    url = os.environ.get("SCOUT_MCP_URL", "http://localhost:8080")
    tenant = os.environ.get("SCOUT_TENANT", "default")
    # Path to the edge proxy's CA certificate, or unset to use the system trust store.
    verify: bool | str = os.environ.get("SCOUT_MCP_CA_BUNDLE") or True
    llm = None
    if command == "ask":
        llm = RedactingLLM(LLM())
        problem = free_model_problem(llm.models)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_USAGE
    token = os.environ.get("SCOUT_MCP_TOKEN")
    if not token:
        token_url = os.environ.get("SCOUT_TOKEN_URL")
        client_id = os.environ.get("SCOUT_CLIENT_ID")
        client_secret = os.environ.get("SCOUT_CLIENT_SECRET")
        if not (token_url and client_id and client_secret):
            print("error: set SCOUT_MCP_TOKEN, or SCOUT_TOKEN_URL with SCOUT_CLIENT_ID "
                  "and SCOUT_CLIENT_SECRET", file=sys.stderr)
            return EXIT_USAGE
        token = await fetch_token(token_url, client_id, client_secret, verify=verify)

    now = datetime.now(timezone.utc)
    async with ScoutMCPClient(url, token, tenant=tenant, verify=verify) as mcp:
        if command == "brief":
            text, code = await brief_command(mcp, argument, now)
        else:
            text, code = await ask_command(SupportCopilot(mcp, llm=llm), mcp, argument, Ledger(), now)
    print(text)
    return code


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("brief", "ask"):
        command, rest = args[0], args[1:]
    else:
        command, rest = "ask", args
    if not rest or (command == "brief" and len(rest) != 1):
        print("usage: scout-copilot brief CUST-1234 | scout-copilot [ask] \"<question>\"", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    sys.exit(asyncio.run(_main(command, " ".join(rest))))


if __name__ == "__main__":
    main()
