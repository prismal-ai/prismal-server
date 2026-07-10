# prismal-server — reference host image (RHB-05-04).
#
# Two-stage build with uv: the builder resolves the engine + host into a self-
# contained /app/.venv; the runtime stage copies only that venv onto a slim
# Python base and runs uvicorn as a non-root user. The engine (prismal-ai) needs
# Python >=3.13, so the base is pinned to 3.13.
#
# Build:  docker build -t prismal-server:dev .
# Run:    docker run -p 8000:8000 prismal-server:dev
# Health: GET /healthz (liveness) · GET /readyz (default runtime composed)

# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

# uv, pinned for reproducible builds (matches CI's UV_VERSION).
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install into a project-local venv. Copy the metadata the build backend needs
# (hatchling reads README/LICENSE) before the source, so the layer caches on
# dependency-only changes.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python .

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# curl is used by the container HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRISMAL_SERVER_HOST=0.0.0.0 \
    PRISMAL_SERVER_PORT=8000

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

# Engine runtime artifacts (sqlite checkpointer, vector store) live here; own it
# so the non-root user can write, and mount a volume over it in production.
RUN mkdir -p /app/data && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://localhost:${PRISMAL_SERVER_PORT}/healthz" || exit 1

# Shell form so ${PRISMAL_SERVER_*} expand; single worker keeps the per-process
# RuntimeContext registry authoritative (scale out with replicas, not workers).
CMD uvicorn prismal_server.app:app --host "$PRISMAL_SERVER_HOST" --port "$PRISMAL_SERVER_PORT"
