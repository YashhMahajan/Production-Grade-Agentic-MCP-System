"""Scout-MCP — a production-grade MCP server for enterprise agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("scout-mcp")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"
