"""Optional LangSmith tracing that sends the shape of a run and none of its content.

Off unless ``SCOUT_LANGSMITH=1`` and ``LANGSMITH_API_KEY`` are both set. When on, the copilot attaches
its own tracer to the graph with a client that reduces everything before it leaves:

* inputs and outputs keep their keys and the length of each string, never the text;
* metadata keeps an allow-list (the command, the prompt version, the model, the step) and drops the rest;
* the tenant appears only as an HMAC under ``SCOUT_TRACE_SALT``, and not at all without one.

So a trace shows which steps ran, in what order, how long each took and how many characters moved, and
a person looking at it learns nothing about a customer. Exception messages are sent as they were raised;
no code here puts customer text in one. LangChain's ambient tracing, which would send content, is switched
off whatever the shell says.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

PROMPT_VERSION = "1"
# What a trace may say about a run, besides the names of its steps.
ALLOWED_METADATA = frozenset({
    "command", "prompt_version", "tenant",
    "ls_provider", "ls_model_name", "ls_model_type", "langgraph_node", "langgraph_step",
})
_AMBIENT = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")
_SHOWN = 5  # how many items of a list keep their shape


def structure(value: Any) -> Any:
    """A copy of ``value`` with its keys and shape and none of its content."""
    if isinstance(value, dict):
        return {str(k): structure(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        shown = [structure(v) for v in value[:_SHOWN]]
        return shown + ([f"<{len(value) - _SHOWN} more>"] if len(value) > _SHOWN else [])
    if isinstance(value, str):
        return f"<str {len(value)}>"
    if value is None or isinstance(value, bool):
        return value
    return f"<{type(value).__name__}>"


def allowed_metadata(metadata: dict) -> dict:
    return {k: v for k, v in metadata.items() if k in ALLOWED_METADATA}


def tenant_label(tenant: str) -> str | None:
    """A stable label that does not name the tenant, or None when no salt is set."""
    salt = os.environ.get("SCOUT_TRACE_SALT")
    return hmac.new(salt.encode(), tenant.encode(), hashlib.sha256).hexdigest()[:12] if salt else None


def enabled() -> bool:
    return os.environ.get("SCOUT_LANGSMITH") == "1" and bool(os.environ.get("LANGSMITH_API_KEY"))


def disable_ambient_tracing() -> None:
    """Keep LangChain's own tracing off: it would send content, with none of the reduction above."""
    for var in _AMBIENT:
        os.environ[var] = "false"


def build_client():
    """A LangSmith client that reduces everything before it is sent."""
    from langsmith import Client

    return Client(api_key=os.environ["LANGSMITH_API_KEY"], hide_inputs=structure, hide_outputs=structure,
                  hide_metadata=allowed_metadata)


def trace_config(command: str, tenant: str) -> dict | None:
    """The ``config`` that makes a graph run traced, or None when tracing is off."""
    disable_ambient_tracing()
    if not enabled():
        return None
    from langchain_core.tracers.langchain import LangChainTracer

    tracer = LangChainTracer(client=build_client(), project_name=os.environ.get("LANGSMITH_PROJECT", "scout-copilot"))
    metadata = {"command": command, "prompt_version": PROMPT_VERSION}
    if label := tenant_label(tenant):
        metadata["tenant"] = label
    return {"callbacks": [tracer], "metadata": metadata, "tags": [command]}


def flush(config: dict | None) -> None:
    """Send what is queued. A short-lived process would otherwise exit before the traces leave."""
    for callback in (config or {}).get("callbacks", []):
        callback.wait_for_futures()
