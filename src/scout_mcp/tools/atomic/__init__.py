"""Level-1 atomic tools — one primitive each, one backend each."""

from scout_mcp.tools.atomic.elasticsearch import ElasticsearchSearchTool
from scout_mcp.tools.atomic.embeddings import EmbeddingClient
from scout_mcp.tools.atomic.http_client import HTTPFetchTool
from scout_mcp.tools.atomic.postgres import PostgresQueryTool
from scout_mcp.tools.atomic.s3_storage import S3GetTool, S3PutTool
from scout_mcp.tools.atomic.vector_search import VectorSearchTool

__all__ = [
    "ElasticsearchSearchTool",
    "EmbeddingClient",
    "HTTPFetchTool",
    "PostgresQueryTool",
    "S3GetTool",
    "S3PutTool",
    "VectorSearchTool",
]
