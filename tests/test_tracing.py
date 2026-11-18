"""Optional LangSmith tracing: off by default, and when on it sends shape, never content."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from scout_mcp.agents import tracing

SECRETS = ("Ana Reyes", "ana@example.com", "Refund delayed", "o_9021", "49.99", "refund_pending")


# ── what leaves a run ──────────────────────────────────────────────────────
def test_structure_keeps_keys_and_the_length_of_strings_and_nothing_else():
    payload = {"customer": {"name": "Ana Reyes", "email": "ana@example.com", "orders": [{"id": "o_9021", "total": 49.99}]},
               "ok": True, "missing": None, "calls": 3}
    assert tracing.structure(payload) == {
        "customer": {"name": "<str 9>", "email": "<str 15>", "orders": [{"id": "<str 6>", "total": "<float>"}]},
        "ok": True, "missing": None, "calls": "<int>"}


def test_a_long_list_keeps_the_shape_of_its_first_items_and_says_how_many_more():
    shown = tracing.structure(["a", "b", "c", "d", "e", "f", "g"])
    assert shown == ["<str 1>"] * 5 + ["<2 more>"]


@pytest.mark.parametrize("value", [("Ana Reyes", "ana@example.com"), {"messages": [[{"content": "Ana Reyes writes"}]]},
                                   {"k": {"k": {"k": "ana@example.com"}}}, "ana@example.com"])
def test_no_text_survives_however_deep_it_sits(value):
    assert not any(secret in json.dumps(tracing.structure(value)) for secret in SECRETS)


def test_only_allowed_metadata_survives():
    md = {"command": "draft", "prompt_version": "1", "tenant": "abc123", "ls_model_name": "m",
          "langgraph_node": "draft", "customer": "Ana Reyes", "thread_id": "t-1", "revision_id": "r"}
    assert tracing.allowed_metadata(md) == {k: md[k] for k in
                                            ("command", "prompt_version", "tenant", "ls_model_name", "langgraph_node")}


def test_the_tenant_is_labelled_only_when_there_is_a_salt_and_never_by_name(monkeypatch):
    monkeypatch.delenv("SCOUT_TRACE_SALT", raising=False)
    assert tracing.tenant_label("acme") is None
    monkeypatch.setenv("SCOUT_TRACE_SALT", "s1")
    one, other = tracing.tenant_label("acme"), tracing.tenant_label("globex")
    assert len(one) == 12 and int(one, 16) >= 0 and "acme" not in one and one != other
    assert tracing.tenant_label("acme") == one
    monkeypatch.setenv("SCOUT_TRACE_SALT", "s2")
    assert tracing.tenant_label("acme") != one


# ── off unless asked ───────────────────────────────────────────────────────
@pytest.mark.parametrize("flag, key, on", [("1", "k", True), ("1", "", False), ("", "k", False), ("0", "k", False)])
def test_tracing_needs_both_the_flag_and_a_key(monkeypatch, flag, key, on):
    monkeypatch.setenv("SCOUT_LANGSMITH", flag)
    monkeypatch.setenv("LANGSMITH_API_KEY", key)
    assert tracing.enabled() is on


def test_a_shell_that_turns_langchain_tracing_on_does_not_turn_it_on_here(monkeypatch):
    for var in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.setenv(var, "true")
    monkeypatch.delenv("SCOUT_LANGSMITH", raising=False)
    assert tracing.trace_config("draft", "acme") is None
    assert {os.environ[v] for v in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2",
                                    "LANGCHAIN_TRACING")} == {"false"}


def test_flushing_when_tracing_is_off_does_nothing():
    tracing.flush(None)
    tracing.flush({})


# ── on, with the real tracer ───────────────────────────────────────────────
@pytest.fixture
def on(monkeypatch):
    pytest.importorskip("langgraph")
    pytest.importorskip("langsmith")
    monkeypatch.setenv("SCOUT_LANGSMITH", "1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setenv("SCOUT_TRACE_SALT", "salt")


def test_the_client_reduces_inputs_outputs_and_metadata(on):
    client = tracing.build_client()
    assert client._hide_inputs is tracing.structure and client._hide_outputs is tracing.structure
    assert client._hide_metadata is tracing.allowed_metadata


def test_the_graph_config_carries_one_tracer_and_only_safe_metadata(on):
    from langchain_core.tracers.langchain import LangChainTracer
    config = tracing.trace_config("ask", "acme")
    (tracer,) = config["callbacks"]
    assert isinstance(tracer, LangChainTracer) and tracer.project_name == "scout-copilot"
    assert config["metadata"] == {"command": "ask", "prompt_version": tracing.PROMPT_VERSION,
                                  "tenant": tracing.tenant_label("acme")}
    assert config["tags"] == ["ask"]


async def test_a_traced_run_sends_structure_and_no_customer_text(on, monkeypatch):
    from langsmith import Client

    from scout_mcp.agents.graphs.draft import run_draft
    from scout_mcp.agents.mcp_client import ToolResult
    from tests.fakes import FakeMCP
    from tests.fakes_lc import ScriptedChat

    sent: list[dict] = []

    class Recorder(Client):
        """The real client, minus the network: it records what would be sent, after the same reduction."""

        def create_run(self, name, inputs=None, run_type=None, **kwargs):
            metadata = (kwargs.get("extra") or {}).get("metadata") or {}
            sent.append({"name": name, "inputs": self._hide_run_inputs(inputs or {}),
                         "metadata": self._hide_run_metadata(metadata)})

        def update_run(self, run_id, **kwargs):
            sent.append({"outputs": self._hide_run_outputs(kwargs.get("outputs") or {}), "error": kwargs.get("error")})

    monkeypatch.setattr(tracing, "build_client", lambda: Recorder(
        api_key="test-key", auto_batch_tracing=False, hide_inputs=tracing.structure,
        hide_outputs=tracing.structure, hide_metadata=tracing.allowed_metadata))
    context = {"customer_id": "CUST-1001", "profile": {"name": "Ana Reyes", "tier": "gold"},
               "orders": [{"id": "o_9021", "status": "refund_pending", "total_cents": 4999, "currency": "USD",
                           "created_at": "2026-11-01T00:00:00Z"}],
               "tickets": [{"id": "TCK-7", "subject": "Refund delayed", "status": "open", "priority": "high",
                            "created_at": "2026-11-10T09:00:00Z"}]}
    reply = json.dumps({"claims": [{"text": "Order o_9021 totals 49.99 USD and is refund_pending.", "cites": [2]}],
                        "needs_approval": True, "insufficient": False})
    config = tracing.trace_config("draft", "acme")
    result = await run_draft(FakeMCP([ToolResult(ok=True, value=context)]), [ScriptedChat(replies=[reply])], "CUST-1001",
                             "Why is Ana Reyes (ana@example.com) still waiting for 49.99 USD?",
                             datetime(2026, 11, 16, tzinfo=timezone.utc), trace=config)
    tracing.flush(config)

    assert result.outcome == "approved"
    dumped = json.dumps(sent, default=str)
    assert not any(secret in dumped for secret in SECRETS), "customer text reached a trace"
    assert "acme" not in dumped  # the tenant appears only as its label
    names = {item["name"] for item in sent if "name" in item}
    assert {"load_evidence", "draft", "check", "finalize"} <= names  # the steps are visible
    metadata = [item["metadata"] for item in sent if "metadata" in item]
    assert metadata and all(set(md) <= tracing.ALLOWED_METADATA for md in metadata)
    assert any(md.get("command") == "draft" and md.get("tenant") == tracing.tenant_label("acme") for md in metadata)
