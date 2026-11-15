"""``scout-mcp new-tool``: write a plugin package that already meets the tool contract.

The scaffold creates files in a new directory and prints the policy rule the tool needs. It
never edits ``config/policy.yaml``: granting access is a decision for a person, and the rule is
useless until they add it.
"""

from __future__ import annotations

import re
from pathlib import Path

# ``namespace`` or ``namespace.verb``: lower case, so it is safe in a package name and a scope.
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")


class ScaffoldError(Exception):
    pass


_PYPROJECT = '''[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{dist}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["scout-mcp>=0.4"]

[project.entry-points."scout_mcp.tools"]
{entry} = "{package}:{cls}"

[tool.hatch.build.targets.wheel]
packages = ["{package}"]
'''

_TOOL = '''"""{name}, a Scout MCP tool."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from scout_mcp.tools.base import Tool, ToolLevel, ToolMetadata


class {stem}Input(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="What to look up.")


class {stem}Output(BaseModel):
    answer: str


class {cls}(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="{name}",
        description="TODO: one sentence on what this tool returns.",
        level=ToolLevel.ATOMIC,
        scopes_required=("{scope}",),
        cacheable=True,
        cache_ttl_seconds=60,
        timeout_ms=5_000,
        tags=("{namespace}",),
        # True if a result can hold text written by customers or third parties.
        returns_untrusted_data=True,
    )
    input_schema: ClassVar[type[BaseModel]] = {stem}Input
    output_schema: ClassVar[type[BaseModel] | None] = {stem}Output

    async def run(self, tenant: str, args: {stem}Input) -> dict:  # type: ignore[override]
        # Scope every query, key and filter to ``tenant``. The server trusts this method to.
        return {{"answer": f"{{tenant}}: {{args.query}}"}}
'''

_TEST = '''from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.tools.registry import contract_problems

from {package} import {cls}


def test_the_tool_meets_the_scout_tool_contract():
    assert contract_problems({cls}(PolicyEngine(rules=[]))) == []
'''

_POLICY = '''rules:
  - id: support-agents-can-call-{slug}
    subjects: ["role:support_copilot"]
    actions: ["{scope}"]
    resources: ["tenant:*/*"]

# policy_test.yaml cases to add, so the rule is tested like the rest:
cases:
  - name: the copilot may call {name}
    subjects: [agent:copilot, "role:support_copilot"]
    tenant: acme
    action: "{scope}"
    resource: "*"
    expect: allow
    rule: support-agents-can-call-{slug}
  - name: an analyst may not call {name}
    subjects: [user:lee, "role:analyst"]
    tenant: acme
    action: "{scope}"
    resource: "*"
    expect: deny
'''


def scaffold_tool(name: str, directory: Path) -> tuple[Path, str]:
    """Write the package for tool ``name`` under ``directory``. Returns its path and the policy text."""
    if not TOOL_NAME.match(name):
        raise ScaffoldError(f"{name!r} is not a tool name: use lower case such as weather or weather.lookup")
    parts = name.split(".")
    slug = "-".join(parts)
    package = "scout_tool_" + "_".join(parts)
    stem = "".join(p.title().replace("_", "") for p in parts)
    fields = dict(
        name=name, namespace=parts[0], slug=slug, package=package, dist=f"scout-tool-{slug}",
        entry=slug, stem=stem, cls=f"{stem}Tool", scope=f"tool:{parts[0]}:read",
    )
    root = directory / fields["dist"]
    if root.exists():
        raise ScaffoldError(f"{root} already exists; nothing was written")
    (root / package).mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(_PYPROJECT.format(**fields))
    (root / package / "__init__.py").write_text(_TOOL.format(**fields))
    (root / "tests" / f"test_{package}.py").write_text(_TEST.format(**fields))
    return root, _POLICY.format(**fields)
