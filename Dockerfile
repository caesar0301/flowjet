# syntax=docker/dockerfile:1
# FlowJet — unified agent CLI + server. This image builds the server half
# (ACP over WebSocket + OpenAI-compatible HTTP on port 8618).
#
# Build:
#   docker build -t flowjet-server:latest .
#
# Optional: use a custom Python base (e.g. internal mirror):
#   --build-arg PYTHON_BASE=registry.example.com/python:3.12-bookworm
#
# Run:
#   docker run --rm -p 8618:8618 \
#     -e DASHSCOPE_API_KEY=... \
#     -e DASHSCOPE_BASE_URL=... \
#     flowjet-server:latest

ARG PYTHON_BASE=registry.cn-hangzhou.aliyuncs.com/lacogito/python:3.12-bookworm

# =============================================================================
# Stage 1: install deps + package into a venv
# =============================================================================
FROM ${PYTHON_BASE} AS deps

# Optional: local wheels to prefer over the registry (e.g. a locally-built
# soothe with unreleased changes). Mount the directory as build context
# `wheels/` and set LOCAL_WHEELS=1; uv then resolves soothe/soothe-sdk from
# these wheels before falling back to PyPI.
ARG LOCAL_WHEELS=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    SOOTHE_HOME=/flowjet

RUN sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv directly via curl (avoids ghcr.io access for networks with restrictions)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

# Metadata first so dependency layer stays cacheable across source edits
COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src
# Local wheel overrides (optional; present only in LOCAL_WHEELS=1 builds)
COPY wheels* /tmp/wheels/

RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    uv venv /app/.venv; \
    # The image only needs the HTTP service; --extra server pulls fastapi,
    # uvicorn, agent-client-protocol, the S3 sync backend, and the boxlite
    # microVM SDK (session isolation).
    UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --no-dev --frozen --no-editable --extra server; \
    if [ "$LOCAL_WHEELS" = "1" ]; then \
      # Force-install local wheel overrides (e.g. a locally-built soothe with
      # unreleased changes) on top of the locked resolution.
      uv pip install --python /app/.venv /tmp/wheels/*.whl; \
    fi

ENV PATH="/app/.venv/bin:${PATH}"

# =============================================================================
# Stage 2: minimal runtime (venv + default nano config, non-root)
# =============================================================================
FROM ${PYTHON_BASE} AS runtime

LABEL org.opencontainers.image.title="FlowJet Server" \
    org.opencontainers.image.description="OpenAI-compatible HTTP service over soothe-nano" \
    org.opencontainers.image.source="https://github.com/mirasoth/flowjet-server"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOOTHE_HOME=/flowjet \
    FLOWJET_HOME=/flowjet \
    FLOWJET_HOST=0.0.0.0 \
    FLOWJET_PORT=8618

WORKDIR /app

COPY --from=deps /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# Default nano config, read by the service from $FLOWJET_HOME/config/nano.yml.
RUN mkdir -p /flowjet/config
COPY config/nano.yml /flowjet/config/nano.yml

# Headless browser for tarzi WebDriver mode — wizsearch's duckduckgo/bing
# scrape engines and JS-heavy crawl targets need it (Tavily API does not).
# Debian keeps chromium & chromium-driver (chromedriver) version-matched.
# tarzi launches chrome with --headless --no-sandbox --disable-gpu
# --disable-dev-shm-usage, which is safe for containers/non-root.
RUN sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --system flowjet \
    && chown -R flowjet:flowjet /flowjet

USER flowjet
WORKDIR /flowjet

EXPOSE 8618/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8618/health', timeout=3)" || exit 1

CMD ["flowjet-server"]
