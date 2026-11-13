"""Component 12 — Governance & multi-tenancy."""

from scout_mcp.governance.approval import ApprovalGate, PendingApproval, PendingApprovalError
from scout_mcp.governance.tenant import TenantMiddleware

__all__ = [
    "ApprovalGate",
    "PendingApproval",
    "PendingApprovalError",
    "TenantMiddleware",
]
