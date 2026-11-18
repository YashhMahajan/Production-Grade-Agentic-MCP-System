"""The copilot that sits on top of Scout-MCP.

Scout-MCP exposes tools. This package uses them to help a support agent: a customer brief and
a list of what is new (no model), and cited replies from a model (``draft`` for one customer,
``ask`` for a free-text question).

Both model flows are one LangGraph state graph (``graphs/``): evidence is gathered, a model writes
claims that each cite it, code checks every claim, and a failed check gets one revision. They differ
only in how evidence is gathered. ``draft`` makes one fixed tool call. ``ask`` lets a model propose a
few read-only tools once, and code decides what runs. The step that reads tool results has no
tools, so text in a ticket cannot cause a call. The graph needs the ``copilot`` extra; nothing
the server imports touches it.

What both flows rely on is written by hand and tested here, not left to a framework's defaults:
the MCP client with its deadline and rate-limit handling, the allow-list, redaction, the call
counts and the daily ledger, and the claim checks.
"""
