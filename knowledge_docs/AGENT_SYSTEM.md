# The Multi-Agent System

Atlas-MCP is an infrastructure layer. `src/atlas_mcp/agents/` is the
product layer that sits on top of it. Concretely, it is a **customer-support
copilot** that helps human agents answer tickets faster.

## The problem

Customer-support agents at Acme Commerce answer dozens of tickets a day.
Each ticket requires looking up:

- The customer's profile and tier.
- Their recent orders.
- Their open and recent tickets.
- Relevant help-centre documentation.

That is four separate systems (Postgres, Elasticsearch, Elasticsearch again,
vector store) and four separate queries per ticket. Agents drift between
tools, miss context, and resolve tickets slowly.

We want an AI copilot that:

1. Pulls the right data on its own.
2. Drafts a reply the human can send (or edit) with one click.
3. Never invents facts, policies, prices, or dates.
4. Never commits to a refund or exception without human approval.
5. Is bounded in time, tokens, and tool calls so it stays affordable.

## The four-agent design

A single LLM call that does "retrieve + synthesise + verify" tends to
hallucinate because the model is optimising three objectives at once. We
break it into four roles with narrow contracts:

### Planner — `agents/planner.py`

Input: a customer question.
Output: a structured plan of information needs.

The planner does **not** call tools. It emits JSON like:

```json
{
  "needs": [
    {"id": "n1", "description": "customer tier and recent orders", "priority": 1},
    {"id": "n2", "description": "refund policy passage", "priority": 1}
  ],
  "customer_id_required": true,
  "notes": ""
}
```

We also run a deterministic regex to pull a customer id out of the
question, so the planner is not the only safeguard against ids being
missed.

### Retriever — `agents/retriever.py`

Input: the plan + the original question.
Output: a list of findings.

This is the only agent that calls MCP tools. It runs a **bounded
tool-calling loop** (default max 6 iterations):

```
for iteration in 1..6:
    ask the LLM which tool to call next, given the running conversation
    if LLM says "done": return findings
    call the tool via AtlasMCPClient
    append the result to the conversation
```

Two layers of tool gating:

1. The retriever has a client-side allowlist (`customer.build_context`,
   `semantic_search`, `hybrid_search`, three atomic reads).
2. The MCP server's policy engine enforces the same thing on its side.

Belt and suspenders is the right mindset here. A prompt injection that
convinces the retriever to call `s3.put_object` still hits the server's
"not in allowlist + not authorised" wall.

### Synthesizer — `agents/synthesizer.py`

Input: the question + findings.
Output: a draft reply with `[S1]`, `[S2]` citation anchors.

The prompt is deliberately strict: max 180 words, no sign-offs, never
commit to a refund without mentioning it needs approval. The citations
are parsed out and surfaced so the human can verify each claim.

### Critic — `agents/critic.py`

Input: question + findings + draft.
Output: `approve` or `revise` with specific issues.

The critic is a second LLM call with a different role. It is not there to
rewrite the draft; it is there to *block* bad ones. Flags include:

- Unsupported factual claims.
- Refund / discount / exception promises without an approval id.
- Contradictions with the findings.
- Policy numbers, prices, or dates not present in findings.
- Drafts over 200 words.

If the critic says revise, the synthesizer gets one more shot with the
critic's hints inlined. After that, the draft returns with
`approved=False` and goes to a human for manual handling. No infinite
loops.

## The orchestrator

`agents/orchestrator.py` wires the four agents together:

```python
from atlas_mcp.agents.mcp_client import AtlasMCPClient
from atlas_mcp.agents.orchestrator import SupportCopilot

async with AtlasMCPClient(url, token, tenant="acme") as mcp:
    copilot = SupportCopilot(mcp)
    response = await copilot.answer(
        "Why was the refund on order o_9002 for CUST-1001 delayed?"
    )
    print(response.draft)
    print(response.citations)
    print(response.to_dict())
```

Every agent writes into the same `AgentRun` object, so when the copilot
returns you get:

- The final draft.
- The approval flag.
- Per-run token counts (`tokens_in`, `tokens_out`).
- Tool-call count.
- The full plan, retrieval trace, and critic verdict.
- A `run_id` that joins up with the OTel traces on the MCP server.

That visibility is not a nice-to-have. When an answer goes wrong in
production you need to see exactly which tool was called with which
arguments and what the LLM did with the result. The `to_dict()` payload
is designed to land in your observability pipeline unchanged.

## What we deliberately did not build

- **No LangGraph / CrewAI state machines.** The four-step flow is linear
  with one revise loop. A state machine would add ceremony without
  capability.
- **No auto-generated tool schemas in the LLM prompts.** We pass the
  server's `list_tools` output straight through. The tool descriptions
  are the agent's documentation and we treat them accordingly.
- **No vector-memory persistence out of the box.** `agents/memory.py`
  has the short-term implementation and a deliberate `NotImplementedError`
  on the long-term write path because we want the approval gate wired up
  properly before the copilot starts remembering things about customers.

The point is: everything in this agent layer is there because a production
support copilot needs it. Nothing is there to look impressive in a demo.
