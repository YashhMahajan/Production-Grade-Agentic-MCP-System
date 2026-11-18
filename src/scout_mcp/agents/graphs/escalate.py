"""``escalate``: ask for a ticket to be raised to high priority, after a person confirms.

No model is involved. LangGraph is here for one thing, a pause:

    lookup -> confirm -> submit
                 |
              interrupt: the graph stops here until the person at the terminal answers

``lookup`` reads the ticket, so nothing is proposed for a ticket that does not exist for this
tenant, and the person sees what they are confirming. ``submit`` calls ``ticket.escalate``, which the
server holds for a reviewer to approve: the copilot has no way to approve, so the outcome of a
confirmed run is a pending approval, not a change. After a reviewer approves, running the same
command with ``approval_id`` skips the question (a person already decided) and makes the approved
call, which the server runs once.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class EscalateState(TypedDict, total=False):
    ticket_id: str
    reason: str
    approval_id: str | None  # given when resuming after a reviewer approved; set when a request is held
    proposal: str
    confirmed: bool
    outcome: str  # pending_approval | done | cancelled | not_found | error
    detail: str


@dataclass
class EscalateResult:
    outcome: str
    detail: str
    approval_id: str | None
    proposal: str | None


def build_escalate_graph(mcp):
    async def lookup(state: EscalateState) -> dict:
        result = await mcp.call_tool("elasticsearch.search", {
            "index": "tickets", "query": {"ids": {"values": [state["ticket_id"]]}}, "size": 1,
            "fields": ["subject", "status", "priority", "customer_id"]})
        if not result.ok:
            return {"outcome": "error", "detail": f"could not look up the ticket: {result.error_code}"}
        if not result.value["hits"]:
            return {"outcome": "not_found", "detail": f"no ticket {state['ticket_id']} for this tenant"}
        t = result.value["hits"][0]["source"]
        return {"proposal": (f"Escalate ticket {state['ticket_id']} ({t.get('priority')} priority, {t.get('status')}, "
                             f"customer {t.get('customer_id')}): {t.get('subject')}\nReason: {state['reason']}")}

    async def confirm(state: EscalateState) -> dict:
        # The node runs again from its first line when the graph resumes, so nothing above this has an effect.
        return {"confirmed": bool(interrupt({"proposal": state["proposal"]}))}

    async def submit(state: EscalateState) -> dict:
        approval_id = state.get("approval_id")
        if not approval_id and not state.get("confirmed"):
            return {"outcome": "cancelled", "detail": "nothing was sent"}
        args = {"ticket_id": state["ticket_id"], "reason": state["reason"]}
        result = await mcp.call_tool("ticket.escalate", {**args, "approval_id": approval_id} if approval_id else args)
        if result.ok:
            return {"outcome": "done", "detail": f"ticket {state['ticket_id']} is now {result.value['priority']} priority"}
        if result.error_code == "pending_approval":
            return {"outcome": "pending_approval", "approval_id": (result.context or {}).get("approval_id"),
                    "detail": "held for a reviewer"}
        return {"outcome": "error", "detail": f"{result.error_code}: {result.hint}"}

    def after_lookup(state: EscalateState) -> str:
        if state.get("outcome"):
            return "end"
        return "submit" if state.get("approval_id") else "confirm"

    g = StateGraph(EscalateState)
    for name, fn in (("lookup", lookup), ("confirm", confirm), ("submit", submit)):
        g.add_node(name, fn)
    g.add_edge(START, "lookup")
    g.add_conditional_edges("lookup", after_lookup, {"end": END, "confirm": "confirm", "submit": "submit"})
    g.add_edge("confirm", "submit")
    g.add_edge("submit", END)
    return g.compile(checkpointer=InMemorySaver())


async def run_escalate(mcp, ticket_id: str, reason: str, confirm: Callable[[str], Awaitable[bool]],
                       approval_id: str | None = None) -> EscalateResult:
    """Run the graph, asking ``confirm`` with the proposal when it pauses."""
    graph = build_escalate_graph(mcp)
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}
    state = await graph.ainvoke(
        {"ticket_id": ticket_id, "reason": reason, "approval_id": approval_id}, config)
    while "__interrupt__" in state:
        answer = await confirm(state["__interrupt__"][0].value["proposal"])
        state = await graph.ainvoke(Command(resume=answer), config)
    return EscalateResult(outcome=state["outcome"], detail=state.get("detail", ""),
                          approval_id=state.get("approval_id"), proposal=state.get("proposal"))
