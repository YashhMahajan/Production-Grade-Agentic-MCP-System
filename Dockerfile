# syntax=docker/dockerfile:1.6
#
# Two-stage build: a fat builder that compiles wheels, then a slim runtime
# image that only carries what is needed to serve.

# ── Stage 1: builder ─────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip build && \
    pip wheel --wheel-dir /wheels .

# ── Stage 2: runtime ─────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ATLAS_TRANSPORT=http ATLAS_HTTP_HOST=0.0.0.0 ATLAS_HTTP_PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — production servers never run as root.
RUN useradd --system --uid 10001 --home /app atlas
WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels atlas-mcp && rm -rf /wheels

# Config and log directories.
RUN mkdir -p /app/config /var/log/atlas && chown -R atlas:atlas /app /var/log/atlas
COPY --chown=atlas:atlas config/ /app/config/

USER atlas
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["atlas-mcp"]
