"""A small CLI so you can drive the copilot from the terminal.

Run with::

    scout-copilot brief CUST-1234
    scout-copilot activity CUST-1234 [7d | 2026-11-10]
    scout-copilot draft CUST-1234 "Why was my refund delayed?"
    scout-copilot ask "Why was the refund on order o_9021 for CUST-1234 delayed?"
    scout-copilot "Why was the refund ..."          # a bare question means ask

``brief`` and ``activity`` make no model call and need no LLM key; ``activity`` lists the
orders and tickets created since a date (default: the last 7 days). Tickets are flagged
against ``config/sla.yaml`` (set SCOUT_SLA_FILE to read it from elsewhere). ``draft`` writes a reply whose every
claim cites evidence and is checked in code; it needs SCOUT_LLM_API_KEY and the ``copilot``
extra (``pip install ".[copilot]"``). ``ask`` needs SCOUT_LLM_API_KEY. When the model cannot
be used (no key, the day's allowance spent, an error, a run that hit its call limit) ``ask``
and ``draft`` print the customer brief instead and exit 3.

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
from datetime import datetime, timedelta, timezone

import httpx

from scout_mcp.agents.base import LLM
from scout_mcp.agents.brief import make_activity, make_brief
from scout_mcp.agents.ledger import Ledger
from scout_mcp.agents.mcp_client import ScoutMCPClient, fetch_token
from scout_mcp.agents.orchestrator import SupportCopilot
from scout_mcp.agents.planner import PlannerAgent
from scout_mcp.agents.redact import RedactingLLM
from scout_mcp.agents.sla import Sla, SlaError, TenantSla

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


def parse_since(text: str, now: datetime) -> datetime:
    """``7d`` is seven days before ``now``; anything else is an ISO 8601 date or time."""
    if text.endswith("d") and text[:-1].isdigit():
        return now - timedelta(days=int(text[:-1]))
    since = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return since if since.tzinfo else since.replace(tzinfo=timezone.utc)


async def activity_command(mcp, customer_id: str, since: datetime) -> tuple[str, int]:
    """New orders and tickets since a time, from one tool call. No model."""
    result = await mcp.call_tool("customer.activity_since",
                                 {"customer_id": customer_id, "since": since.isoformat()})
    if not result.ok:
        return f"could not load activity for {customer_id}: {result.error_code}", EXIT_NOT_OK
    return make_activity(result.value), EXIT_OK


async def brief_command(mcp, customer_id: str, now: datetime, sla: TenantSla | None = None) -> tuple[str, int]:
    """The customer brief from one tool call. No model."""
    result = await mcp.call_tool(
        "customer.build_context", {"customer_id": customer_id, "question": "brief"})
    if not result.ok:
        return f"could not load {customer_id}: {result.error_code}", EXIT_NOT_OK
    return make_brief(result.value, now, sla).text, EXIT_OK


async def ask_command(copilot: SupportCopilot, mcp, question: str, ledger: Ledger,
                      now: datetime, sla: TenantSla | None = None) -> tuple[str, int]:
    customer_id = PlannerAgent._extract_customer_id(question)

    async def evidence_pack(why: str) -> tuple[str, int]:
        if not customer_id:
            return f"{why}\nInclude a customer id (e.g. CUST-1234) to get a brief.", EXIT_EVIDENCE
        text, code = await brief_command(mcp, customer_id, now, sla)
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


async def draft_command(mcp, customer_id: str, question: str, ledger: Ledger, now: datetime,
                        models: list | None = None, sla: TenantSla | None = None) -> tuple[str, int]:
    """A cited, checked draft reply. Falls back to the brief when the model cannot be used."""
    from scout_mcp.agents.llm import FinalLLMError

    def pack(why: str, context: dict | None) -> tuple[str, int]:
        body = make_brief(context, now, sla).text if context else "No customer data was loaded."
        return f"{why} Here is the evidence instead.\n\n{body}", EXIT_EVIDENCE

    async def brief_pack(why: str) -> tuple[str, int]:
        text, code = await brief_command(mcp, customer_id, now, sla)
        return (f"{why} Here is the evidence instead.\n\n{text}", EXIT_EVIDENCE) if code == EXIT_OK else (text, code)

    allowed = ledger.remaining()
    if allowed < 1:
        return await brief_pack("Today's model allowance is spent.")
    if models is None:
        try:
            from scout_mcp.agents.llm import build_models
            models = build_models()
        except ImportError:
            return 'draft needs the copilot extra: pip install ".[copilot]"', EXIT_USAGE
        except FinalLLMError as exc:
            return await brief_pack(f"The model could not be used ({exc}).")
    from scout_mcp.agents.graphs.draft import run_draft

    cap = min(int(os.environ.get("SCOUT_DRAFT_MAX_LLM_CALLS", "3")), allowed)
    result = await run_draft(mcp, models, customer_id, question, now, max_llm_calls=cap,
                             timeout_s=float(os.environ.get("SCOUT_AGENT_TIMEOUT_S", "90")), sla=sla)
    ledger.record(result.llm_calls)

    if result.outcome == "error":
        return result.reason or "lookup failed", EXIT_NOT_OK
    if result.outcome == "evidence_pack":
        return pack(f"No draft was written ({result.reason}).", result.context)
    flag = "approved" if result.outcome == "approved" else "flagged: " + "; ".join(result.problems)
    evidence = "\n".join(f"[S{e.id}] {e.text}" for e in result.evidence)
    text = "\n".join([f"DRAFT REPLY ({flag})", "=" * 72, result.text, "", "EVIDENCE", "=" * 72, evidence, "",
                      f"model calls: {result.llm_calls}"])
    return text, EXIT_OK if result.outcome == "approved" else EXIT_NOT_OK


async def _main(command: str, argument: str) -> int:
    url = os.environ.get("SCOUT_MCP_URL", "http://localhost:8080")
    tenant = os.environ.get("SCOUT_TENANT", "default")
    # Path to the edge proxy's CA certificate, or unset to use the system trust store.
    verify: bool | str = os.environ.get("SCOUT_MCP_CA_BUNDLE") or True
    llm = None
    if command == "ask":
        llm = RedactingLLM(LLM())
        names = llm.models
    elif command == "draft":
        from scout_mcp.agents.llm import model_names
        names = model_names()
    if command in ("ask", "draft"):
        problem = free_model_problem(names)
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
    try:
        sla = Sla.load().for_tenant(tenant)
    except FileNotFoundError:
        print("note: no SLA file found (config/sla.yaml or SCOUT_SLA_FILE); tickets will not be flagged",
              file=sys.stderr)
        sla = None
    except SlaError as exc:
        print(f"error: SLA file: {exc}", file=sys.stderr)
        return EXIT_USAGE
    async with ScoutMCPClient(url, token, tenant=tenant, verify=verify) as mcp:
        if command == "brief":
            text, code = await brief_command(mcp, argument, now, sla)
        elif command == "activity":
            customer_id, _, since = argument.partition(" ")
            text, code = await activity_command(mcp, customer_id.upper(), parse_since(since or "7d", now))
        elif command == "draft":
            customer_id, _, question = argument.partition(" ")
            text, code = await draft_command(mcp, customer_id.upper(), question.strip(), Ledger(), now, sla=sla)
        else:
            text, code = await ask_command(SupportCopilot(mcp, llm=llm), mcp, argument, Ledger(), now, sla)
    print(text)
    return code


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("brief", "ask", "draft", "activity"):
        command, rest = args[0], args[1:]
    else:
        command, rest = "ask", args
    needs_id = command in ("draft", "activity") and not (rest and PlannerAgent._extract_customer_id(rest[0]))
    wrong_count = (command == "brief" and len(rest) != 1) or (command == "draft" and len(rest) < 2) \
        or (command == "activity" and len(rest) > 2)
    if not rest or needs_id or wrong_count:
        print("usage: scout-copilot brief CUST-1234 | activity CUST-1234 [7d|DATE] | "
              "draft CUST-1234 \"<question>\" | [ask] \"<question>\"", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    sys.exit(asyncio.run(_main(command, " ".join(rest))))


if __name__ == "__main__":
    main()
