# syntax=docker/dockerfile:1.6

# Two-stage build: a fat builder that compiles wheels, 
# then a slim runtime that only carries what is needed to serve.

# ── Stage 1: builder ─────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY README.md ./
COPY src ./src

RUN pip install --upgrade pip build && \
    pip wheel --wheel-dir /wheels .

# ── Stage 2: runtime ─────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    SCOUT_TRANSPORT=http SCOUT_HTTP_HOST=0.0.0.0 SCOUT_HTTP_PORT=8080 \
    SCOUT_EMBEDDING_CACHE_DIR=/models

# `upgrade` pulls Debian's security fixes that landed after the base image was built
# (zlib1g had two HIGH advisories with fixes available).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates tini \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — production servers never run as root.
RUN useradd --system --uid 10001 --home /app scout
WORKDIR /app

COPY --from=builder /wheels /wheels
# The base image's pip, setuptools and wheel carry advisories of their own (wheel 0.45.1,
# jaraco.context inside setuptools, an older setuptools), and nothing builds at runtime.
# Install the app, then remove the build tooling and pip's bundled install wheels.
RUN pip install --no-index --find-links=/wheels scout-mcp && rm -rf /wheels \
    && pip uninstall -y setuptools wheel pip \
    && rm -rf /usr/local/lib/python3.11/ensurepip/_bundled

# Bake the embedding model into the image (about 210 MB as ONNX). The container then
# starts without network access to Hugging Face, and replicas do not each download it.
RUN mkdir -p /models && python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-base-en-v1.5', cache_dir='/models')"

# Config and log directories.
RUN mkdir -p /app/config /var/log/scout && chown -R scout:scout /app /var/log/scout /models
COPY --chown=scout:scout config/ /app/config/

USER scout
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8080/readyz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["scout-mcp"]
