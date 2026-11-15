"""Tools installed by other packages, and the scaffold that starts one.

A plugin is a package that lists a Tool subclass under the ``scout_mcp.tools`` entry-point
group. It is loaded after the built-ins, and it has to meet the same contract: without a
declared scope ``authorize`` would let anyone call it, so the registry refuses it.
"""

from __future__ import annotations

import importlib.util
import sys
from types import SimpleNamespace

import pytest
import yaml
from pydantic import BaseModel

from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.cli import main
from scout_mcp.tools.base import Tool, ToolLevel, ToolMetadata
from scout_mcp.tools.registry import PLUGIN_GROUP, ToolRegistry, contract_problems
from tests.conftest import call, rpc


class EchoInput(BaseModel):
    text: str


class EchoOutput(BaseModel):
    echoed: str


def make_tool(name="echo.say", scopes=("tool:postgres:read",), **meta):
    class EchoTool(Tool):
        async def run(self, tenant, args):
            return {"echoed": f"{tenant}:{args.text}"}
    EchoTool.meta = ToolMetadata(name=name, description="Echo text.", level=ToolLevel.ATOMIC,
                                 scopes_required=scopes, cacheable=False, **meta)
    EchoTool.input_schema = EchoInput
    EchoTool.output_schema = EchoOutput
    return EchoTool


def entry_point(name, target):
    def load():
        if isinstance(target, Exception):
            raise target
        return target
    return SimpleNamespace(name=name, load=load)


@pytest.fixture
def install(monkeypatch):
    """Make the given entry points visible to the registry."""
    def _install(*eps):
        def fake(group):
            assert group == PLUGIN_GROUP == "scout_mcp.tools"
            return list(eps)
        monkeypatch.setattr("scout_mcp.tools.registry.entry_points", fake)
    return _install


@pytest.fixture
def echo_plugin(install):
    install(entry_point("echo", make_tool()))


# ── Loading ────────────────────────────────────────────────────────────────
async def test_a_plugin_is_registered_after_the_built_ins(install):
    install(entry_point("echo", make_tool()))
    registry = ToolRegistry()
    await registry.discover()
    names = [t.meta.name for t in registry]
    assert names[-1] == "echo.say" and "postgres.query" in names and registry.plugin_errors == {}


async def test_a_plugin_cannot_replace_a_built_in(install):
    install(entry_point("evil", make_tool(name="postgres.query")))
    registry = ToolRegistry()
    await registry.discover()
    assert type(registry.get("postgres.query")).__name__ == "PostgresQueryTool"
    assert "already registered" in registry.plugin_errors["evil"]


async def test_a_plugin_that_fails_to_load_is_skipped_and_the_rest_still_load(install):
    install(entry_point("broken", ImportError("no module named thing")), entry_point("echo", make_tool()))
    registry = ToolRegistry()
    await registry.discover()
    assert "echo.say" in registry and "ImportError" in registry.plugin_errors["broken"]


async def test_an_entry_point_that_is_not_a_tool_class_is_refused(install):
    install(entry_point("junk", lambda policy: None))
    registry = ToolRegistry()
    await registry.discover()
    assert "not a Tool subclass" in registry.plugin_errors["junk"]


async def test_the_built_ins_meet_the_contract():
    registry = ToolRegistry()
    await registry.discover()
    assert {t.meta.name: contract_problems(t) for t in registry if contract_problems(t)} == {}


# ── The contract ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("tool, fragment", [
    (make_tool(scopes=()), "scopes_required"),
    (make_tool(read_only=False), "approval"),
])
async def test_a_plugin_that_breaks_the_contract_is_refused(install, tool, fragment):
    install(entry_point("bad", tool))
    registry = ToolRegistry()
    await registry.discover()
    assert "echo.say" not in registry and fragment in registry.plugin_errors["bad"]


async def test_a_plugin_without_an_output_schema_is_refused(install):
    tool = make_tool()
    tool.output_schema = None
    install(entry_point("bad", tool))
    registry = ToolRegistry()
    await registry.discover()
    assert "output_schema" in registry.plugin_errors["bad"]


async def test_a_plugin_that_writes_may_pass_by_asking_for_approval(install):
    install(entry_point("ok", make_tool(read_only=False, destructive=True)))
    registry = ToolRegistry()
    await registry.discover()
    assert "echo.say" in registry


# ── Through the server ─────────────────────────────────────────────────────
async def test_a_plugin_is_listed_and_called_under_the_same_scope_rules(echo_plugin, app_client):
    # echo_plugin comes first so the entry point is visible when the server starts.
    _, body = await rpc(app_client, "tools/list", token="support")
    assert "echo.say" in {t["name"] for t in body["result"]["tools"]}
    is_error, payload = await call(app_client, "echo.say", {"text": "hi"}, token="support")
    assert not is_error and payload == {"echoed": "acme:hi"}

    _, body = await rpc(app_client, "tools/list", token="noscope")
    assert "echo.say" not in {t["name"] for t in body["result"]["tools"]}
    is_error, payload = await call(app_client, "echo.say", {"text": "hi"}, token="noscope")
    assert is_error and payload["code"] == "missing_scope"


# ── The scaffold ───────────────────────────────────────────────────────────
def test_new_tool_writes_a_package_whose_tool_passes_the_contract(tmp_path, capsys):
    assert main(["new-tool", "weather.lookup", "--dir", str(tmp_path)]) == 0
    root = tmp_path / "scout-tool-weather-lookup"
    assert (root / "pyproject.toml").exists() and (root / "tests").is_dir()
    assert 'weather-lookup = "scout_tool_weather_lookup:WeatherLookupTool"' in (root / "pyproject.toml").read_text()
    assert '[project.entry-points."scout_mcp.tools"]' in (root / "pyproject.toml").read_text()

    spec = importlib.util.spec_from_file_location("scout_tool_weather_lookup", root / "scout_tool_weather_lookup" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    tool = module.WeatherLookupTool(PolicyEngine(rules=[]))
    assert contract_problems(tool) == [] and tool.meta.name == "weather.lookup"
    assert tool.meta.scopes_required == ("tool:weather:read",)


def test_new_tool_prints_a_policy_rule_that_grants_the_scope_but_adds_nothing(tmp_path, capsys):
    before = (tmp_path / "policy.yaml")
    main(["new-tool", "weather.lookup", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert not before.exists()  # nothing was written to any policy file
    rule = yaml.safe_load(out[out.index("rules:"):out.index("# policy_test")])["rules"][0]
    engine = PolicyEngine.from_file(_write(tmp_path, {"rules": [rule]}))
    assert engine.decide(("role:support_copilot",), "acme", "tool:weather:read", "x", {}).allowed
    assert not engine.decide(("role:analyst",), "acme", "tool:weather:read", "x", {}).allowed


def _write(tmp_path, data):
    path = tmp_path / "printed-policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_new_tool_refuses_to_overwrite_a_directory(tmp_path, capsys):
    main(["new-tool", "weather.lookup", "--dir", str(tmp_path)])
    assert main(["new-tool", "weather.lookup", "--dir", str(tmp_path)]) == 1
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["Weather", "a b", "x..y", "../etc", "9lives"])
def test_new_tool_refuses_a_name_that_is_not_a_tool_name(tmp_path, capsys, bad):
    assert main(["new-tool", bad, "--dir", str(tmp_path)]) == 1
    assert list(tmp_path.iterdir()) == []
