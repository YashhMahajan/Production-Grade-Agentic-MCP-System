"""The server never loads the copilot's framework, so its image and its attack surface stay small."""

from __future__ import annotations

import subprocess
import sys

FRAMEWORKS = ("langchain", "langchain_core", "langchain_openai", "langgraph", "langsmith")


def test_importing_the_server_and_the_plain_copilot_loads_no_agent_framework():
    code = (
        "import sys\n"
        "import scout_mcp.server, scout_mcp.agents.cli, scout_mcp.agents.claims, scout_mcp.agents.llm\n"
        f"print([m for m in sys.modules if m.split('.')[0] in {FRAMEWORKS!r}])\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
