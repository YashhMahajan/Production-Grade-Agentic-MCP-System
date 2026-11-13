"""Components 2 & 3 — Authentication and Authorization."""

from scout_mcp.auth.oauth import Principal, TokenValidator, get_validator
from scout_mcp.auth.policy import PolicyEngine, Rule

__all__ = ["Principal", "TokenValidator", "get_validator", "PolicyEngine", "Rule"]
