"""The agents that sit on top of Scout-MCP.

Scout-MCP exposes tools. This package exposes agents that use those tools to solve a
concrete problem end to end: customer support.

There are two paths. ``ask`` is the original hand-written loop: a planner, a tool-calling
retriever, a synthesizer and a critic, each a direct OpenRouter call. ``draft`` is a
LangGraph state graph over fixed steps (load evidence, draft, check, revise) with
LangChain chat models. The graph path lives in ``graphs/`` and needs the ``copilot``
extra; nothing the server imports touches it.

What both paths rely on is written by hand and tested here, not left to a framework's
defaults: the MCP client with its deadline and rate-limit handling, the allow-list,
redaction, the call counts and the daily ledger, and the claim and citation checks.
"""
