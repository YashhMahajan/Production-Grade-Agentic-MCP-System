"""A small CLI so you can drive the copilot from the terminal.

Run with::

    scout-copilot brief CUST-1234
    scout-copilot activity CUST-1234 [7d | 2026-11-10]
    scout-copilot draft CUST-1234 "Why was my refund delayed?"
    scout-copilot ask "Why was the refund on order o_9021 for CUST-1234 delayed?"
    scout-copilot escalate t_5002 "Past its SLA and the customer has written twice" [--yes]
    scout-copilot escalate t_5002 "<same reason>" --approval APPROVAL_ID
    scout-copilot "Why was the refund ..."          # a bare question means ask

``brief`` and ``activity`` make no model call and need no LLM key; ``activity`` lists the
orders and tickets created since a date (default: the last 7 days). Tickets are flagged against
``config/sla.yaml`` (set SCOUT_SLA_FILE to read it from elsewhere).

``draft`` and ``ask`` write a reply whose every claim cites evidence and is checked in code. They
need SCOUT_LLM_API_KEY and the ``copilot`` extra (``pip install ".[copilot]"``). ``draft`` makes one
fixed tool call for one customer; ``ask`` lets the model choose a few read-only tools for a
free-text question. When the model cannot be used (no key, the day's allowance spent, an error, a
run that hit its call limit) both print the customer brief instead and exit 3.

``escalate`` asks for a ticket to be raised to high priority. It makes no model call. It shows the
ticket and the reason, asks you to confirm (``--yes`` skips the question), and sends the request;
the server holds it until a reviewer approves, because the copilot cannot. After the approval, run
the same command with ``--approval ID`` to make the approved call.

All of them need SCOUT_MCP_URL and SCOUT_TENANT, plus either SCOUT_MCP_TOKEN or SCOUT_TOKEN_URL
with SCOUT_CLIENT_ID and SCOUT_CLIENT_SECRET (the Keycloak client in deploy/keycloak).
SCOUT_MCP_CA_BUNDLE points at the edge proxy's CA certificate.

Exit codes: 0 answered (or an escalation sent), 1 not approved, not sent or the lookup failed, 2 bad usage or settings,
3 evidence pack instead of a model answer.
"""

#  this is to debug and run on local machine without docker
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx

from scout_mcp.agents.brief import make_activity, make_brief
from scout_mcp.agents.ids import extract_customer_id
from scout_mcp.agents.ledger import Ledger
from scout_mcp.agents.mcp_client import ScoutMCPClient, fetch_token
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


async def _confirm_at_the_terminal(proposal: str) -> bool:
    print(proposal)
    try:
        answer = await asyncio.to_thread(input, "Send this for approval? [y/N] ")
    except EOFError:  # no terminal to ask: the answer is no
        return False
    return answer.strip().lower() in ("y", "yes")


async def _confirm_without_asking(proposal: str) -> bool:
    print(proposal)
    return True


async def escalate_command(mcp, ticket_id: str, reason: str, confirm, approval_id: str | None = None) -> tuple[str, int]:
    """Ask for a ticket to be escalated, after ``confirm`` agrees. No model."""
    try:
        from scout_mcp.agents.graphs.escalate import run_escalate
    except ImportError:
        return 'this command needs the copilot extra: pip install ".[copilot]"', EXIT_USAGE
    try:
        result = await run_escalate(mcp, ticket_id, reason, confirm, approval_id)
    except httpx.HTTPError as exc:
        return f"The tools could not be reached ({type(exc).__name__}).", EXIT_NOT_OK
    if result.outcome == "pending_approval":
        return (f"Sent. A reviewer has to approve it (approval {result.approval_id}); the copilot cannot. "
                f"Once approved, run:\n  scout-copilot escalate {ticket_id} \"{reason}\" --approval {result.approval_id}",
                EXIT_OK)
    if result.outcome == "done":
        return result.detail, EXIT_OK
    return result.detail, EXIT_NOT_OK


def _render(result) -> tuple[str, int]:
    flag = "approved" if result.outcome == "approved" else "flagged: " + "; ".join(result.problems)
    evidence = "\n".join(f"[S{e.id}] {e.text}" for e in result.evidence)
    lines = [f"DRAFT REPLY ({flag})", "=" * 72, result.text, "", "EVIDENCE", "=" * 72, evidence]
    if result.errors:
        lines += ["", "Tool calls that returned no evidence: " + "; ".join(result.errors)]
    lines += ["", f"model calls: {result.llm_calls}, tool calls: {result.tool_calls}"]
    return "\n".join(lines), EXIT_OK if result.outcome == "approved" else EXIT_NOT_OK


async def _with_model(mcp, customer_id: str, ledger: Ledger, now: datetime, sla: TenantSla | None,
                      models: list | None, cap_var: str, default_cap: int, run) -> tuple[str, int]:
    """Run one model flow under the day's allowance, falling back to the brief when it cannot finish.

    ``run(models, cap, timeout_s)`` returns a ``DraftResult``.
    """
    from scout_mcp.agents.llm import FinalLLMError

    async def evidence_pack(why: str, context: dict | None = None) -> tuple[str, int]:
        if context:
            body = make_brief(context, now, sla).text
        elif customer_id:
            body, code = await brief_command(mcp, customer_id, now, sla)
            if code != EXIT_OK:
                return body, code
        else:
            return f"{why}\nInclude a customer id (e.g. CUST-1234) to get a brief.", EXIT_EVIDENCE
        return f"{why} Here is the evidence instead.\n\n{body}", EXIT_EVIDENCE

    allowed = ledger.remaining()
    if allowed < 1:
        return await evidence_pack("Today's model allowance is spent.")
    if models is None:
        try:
            from scout_mcp.agents.llm import build_models
            models = build_models()
        except ImportError:
            return 'this command needs the copilot extra: pip install ".[copilot]"', EXIT_USAGE
        except FinalLLMError as exc:
            return await evidence_pack(f"The model could not be used ({exc}).")
    cap = min(int(os.environ.get(cap_var, default_cap)), allowed)
    try:
        result = await run(models, cap, float(os.environ.get("SCOUT_AGENT_TIMEOUT_S", "90")))
    except httpx.HTTPError as exc:
        # The brief needs the same tools, so there is nothing to show instead.
        return f"The tools could not be reached ({type(exc).__name__}).", EXIT_NOT_OK
    ledger.record(result.llm_calls)

    if result.outcome == "error":
        return result.reason or "lookup failed", EXIT_NOT_OK
    if result.outcome == "evidence_pack":
        return await evidence_pack(f"No answer was written ({result.reason}).", result.context)
    return _render(result)


async def draft_command(mcp, customer_id: str, question: str, ledger: Ledger, now: datetime,
                        models: list | None = None, sla: TenantSla | None = None) -> tuple[str, int]:
    """A cited, checked reply for one customer, from one fixed tool call."""
    async def run(models, cap, timeout_s):
        from scout_mcp.agents.graphs.draft import run_draft
        return await run_draft(mcp, models, customer_id, question, now, max_llm_calls=cap,
                               timeout_s=timeout_s, sla=sla)

    return await _with_model(mcp, customer_id, ledger, now, sla, models, "SCOUT_DRAFT_MAX_LLM_CALLS", 3, run)


async def ask_command(mcp, question: str, ledger: Ledger, now: datetime,
                      models: list | None = None, sla: TenantSla | None = None) -> tuple[str, int]:
    """A cited, checked answer to a free-text question, from tools the model picks once."""
    async def run(models, cap, timeout_s):
        from scout_mcp.agents.graphs.ask import run_ask
        return await run_ask(mcp, models, question, now, max_llm_calls=cap, timeout_s=timeout_s, sla=sla)

    return await _with_model(mcp, extract_customer_id(question) or "", ledger, now, sla, models,
                             "SCOUT_AGENT_MAX_LLM_CALLS", 6, run)


async def _main(command: str, argument: str, *, yes: bool = False, approval_id: str | None = None) -> int:
    url = os.environ.get("SCOUT_MCP_URL", "http://localhost:8080")
    tenant = os.environ.get("SCOUT_TENANT", "default")
    # Path to the edge proxy's CA certificate, or unset to use the system trust store.
    verify: bool | str = os.environ.get("SCOUT_MCP_CA_BUNDLE") or True
    if command in ("ask", "draft"):
        from scout_mcp.agents.llm import model_names
        problem = free_model_problem(model_names())
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
        elif command == "escalate":
            ticket_id, _, reason = argument.partition(" ")
            confirm = _confirm_without_asking if yes else _confirm_at_the_terminal
            text, code = await escalate_command(mcp, ticket_id, reason.strip(), confirm, approval_id)
        elif command == "draft":
            customer_id, _, question = argument.partition(" ")
            text, code = await draft_command(mcp, customer_id.upper(), question.strip(), Ledger(), now, sla=sla)
        else:
            text, code = await ask_command(mcp, argument, Ledger(), now, sla=sla)
    print(text)
    return code


_TICKET_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _usage() -> None:
    print("usage: scout-copilot brief CUST-1234 | activity CUST-1234 [7d|DATE] | "
          "draft CUST-1234 \"<question>\" | [ask] \"<question>\" | "
          "escalate TICKET \"<reason>\" [--yes] [--approval ID]", file=sys.stderr)
    sys.exit(EXIT_USAGE)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("brief", "ask", "draft", "activity", "escalate"):
        command, rest = args[0], args[1:]
    else:
        command, rest = "ask", args
    yes, approval_id = False, None
    if command == "escalate":
        yes = "--yes" in rest
        rest = [a for a in rest if a != "--yes"]
        if "--approval" in rest:
            i = rest.index("--approval")
            approval_id, rest = (rest[i + 1] if i + 1 < len(rest) else None), rest[:i] + rest[i + 2:]
            if not approval_id:
                _usage()
    needs_id = command in ("draft", "activity") and not (rest and extract_customer_id(rest[0]))
    wrong_count = (command == "brief" and len(rest) != 1) or (command == "draft" and len(rest) < 2) \
        or (command == "activity" and len(rest) > 2) or (command == "escalate" and len(rest) < 2)
    bad_ticket = command == "escalate" and rest and not _TICKET_ID.fullmatch(rest[0])
    if not rest or needs_id or wrong_count or bad_ticket:
        _usage()
    sys.exit(asyncio.run(_main(command, " ".join(rest), yes=yes, approval_id=approval_id)))


if __name__ == "__main__":
    main()
