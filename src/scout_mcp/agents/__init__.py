"""The multi-agent layer that sits on top of Scout-MCP.

Scout-MCP exposes tools. This package exposes *agents that use those tools*
to solve a concrete enterprise problem end-to-end: customer support triage.

The agents are deliberately simple and explicit. There is no heavy
framework here — no LangGraph state machines, no CrewAI role play — just
direct OpenRouter API calls with clear prompts and a small orchestrator
that runs them in sequence with a bounded critique loop.

This is the pattern that survives production: easy to debug, easy to
change, easy to swap out one agent without rewriting the others.
"""
