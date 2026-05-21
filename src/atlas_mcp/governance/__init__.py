"""Component 12 — Governance & multi-tenancy."""

from atlas_mcp.governance.approval import ApprovalGate, PendingApproval, PendingApprovalError
from atlas_mcp.governance.tenant import TenantMiddleware

__all__ = [
    "ApprovalGate",
    "PendingApproval",
    "PendingApprovalError",
    "TenantMiddleware",
]
