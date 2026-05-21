"""Atomic S3 storage tool.

Reads and writes objects in a single bucket. Writes are tenant-prefixed so
that even if a policy bug lets tenant A call put_object without a filter,
it still cannot overwrite tenant B's data — the bucket layout makes that
physically impossible.

Write is destructive and goes through the approval gate by default.
Read-only access is available to agents without approval.
"""

from __future__ import annotations

from typing import Any, ClassVar

import aioboto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from atlas_mcp.auth.policy import PolicyEngine
from atlas_mcp.config import get_settings
from atlas_mcp.errors.framework import UpstreamError
from atlas_mcp.tools.base import Tool, ToolLevel, ToolMetadata


# ── Get (read) ────────────────────────────────────────────────────────────
class S3GetInput(BaseModel):
    key: str = Field(..., description="Object key — will be prefixed with tenant/.")
    max_bytes: int = Field(1_048_576, ge=1, le=10_485_760)  # 1 MiB default, 10 MiB ceiling


class S3GetTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="s3.get_object",
        description="Read a text object from the Atlas S3 bucket.",
        level=ToolLevel.ATOMIC,
        scopes_required=("tool:s3:read",),
        cacheable=True,
        cache_ttl_seconds=300,
        timeout_ms=3_000,
        tags=("s3", "storage", "read"),
    )
    input_schema: ClassVar[type[BaseModel]] = S3GetInput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.settings = get_settings()
        self._session = aioboto3.Session()

    async def run(self, tenant: str, args: S3GetInput) -> dict:  # type: ignore[override]
        prefixed_key = _tenant_key(tenant, args.key)
        try:
            async with self._session.client("s3", endpoint_url=self.settings.s3_endpoint) as s3:
                resp = await s3.get_object(
                    Bucket=self.settings.s3_bucket,
                    Key=prefixed_key,
                    Range=f"bytes=0-{args.max_bytes - 1}",
                )
                body = await resp["Body"].read()
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "NoSuchKey":
                raise UpstreamError(
                    "not_found", retryable=False, hint=f"s3 key {args.key!r} does not exist"
                ) from exc
            raise UpstreamError(
                "s3_error", retryable=code in ("InternalError", "SlowDown"), hint=str(exc)
            ) from exc
        return {"key": args.key, "size_bytes": len(body), "content": body.decode("utf-8", errors="replace")}


# ── Put (write) ───────────────────────────────────────────────────────────
class S3PutInput(BaseModel):
    key: str = Field(..., description="Object key — will be prefixed with tenant/.")
    content: str = Field(..., description="Text content to store.")
    content_type: str = Field("text/plain", description="MIME type for the uploaded object.")


class S3PutTool(Tool):
    meta: ClassVar[ToolMetadata] = ToolMetadata(
        name="s3.put_object",
        description="Write a text object to the Atlas S3 bucket. Requires human approval.",
        level=ToolLevel.ATOMIC,
        scopes_required=("tool:s3:write",),
        destructive=True,
        cacheable=False,
        timeout_ms=5_000,
        tags=("s3", "storage", "write"),
    )
    input_schema: ClassVar[type[BaseModel]] = S3PutInput

    def __init__(self, policy: PolicyEngine):
        super().__init__(policy)
        self.settings = get_settings()
        self._session = aioboto3.Session()

    async def run(self, tenant: str, args: S3PutInput) -> dict:  # type: ignore[override]
        prefixed_key = _tenant_key(tenant, args.key)
        try:
            async with self._session.client("s3", endpoint_url=self.settings.s3_endpoint) as s3:
                await s3.put_object(
                    Bucket=self.settings.s3_bucket,
                    Key=prefixed_key,
                    Body=args.content.encode("utf-8"),
                    ContentType=args.content_type,
                    Metadata={"tenant": tenant},
                )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            raise UpstreamError(
                "s3_error", retryable=code in ("InternalError", "SlowDown"), hint=str(exc)
            ) from exc
        return {"key": args.key, "bytes_written": len(args.content.encode("utf-8"))}


def _tenant_key(tenant: str, key: str) -> str:
    """Prefix every object with the tenant id so the layout enforces isolation."""
    return f"{tenant}/{key.lstrip('/')}"
