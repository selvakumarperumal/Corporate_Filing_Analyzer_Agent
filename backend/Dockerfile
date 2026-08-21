# syntax=docker/dockerfile:1

# Corporate Filing Analyzer — API image (FastAPI + Socket.IO under uvicorn).
#
# The build context is `backend/`, not the repo root, because the app resolves
# its prompts, logs and data directories relative to it: every path in the code
# is `Path(__file__).resolve().parents[2]`, which is this directory. Keeping the
# `Analyzer/` + `config/` layout intact inside the image is what makes those
# paths land in the right place without a single code change.

# ── Stage 1: resolve dependencies ────────────────────────────────────────────
# Split from the runtime stage so the (slow, rarely-invalidated) dependency
# install is not redone every time a source file changes.
FROM python:3.14-slim-bookworm AS builder

# uv is copied in rather than pip-installed: same resolver and lockfile as
# local development, and it keeps the interpreter identical across both stages
# so the virtualenv built here is valid in the runtime image.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app/Analyzer

# Lockfile only, ahead of the source: this layer is cached until the
# dependencies themselves change.
COPY Analyzer/pyproject.toml Analyzer/uv.lock ./

# --frozen fails the build if uv.lock has drifted from pyproject.toml, so an
# image can never be built from dependencies nobody resolved.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/Analyzer/.venv/bin:$PATH"

# An unprivileged account to run as. Nothing the app does needs root, and a
# container that never needs it should never have it.
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app

WORKDIR /app/Analyzer

COPY --from=builder /app/Analyzer/.venv /app/Analyzer/.venv

# The two directories the code expects beside each other under `backend/`.
COPY Analyzer/ /app/Analyzer/
COPY config/ /app/config/

# Created here (not left to the volume mount) so the image also runs standalone
# with no volumes attached, and so they are owned by the app user either way.
RUN mkdir -p /app/data /app/logs && chown -R app:app /app

USER app

EXPOSE 8000

# Hits the one route that is deliberately unauthenticated. Written in Python
# because the slim image ships no curl or wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

# `asgi_app` is the Socket.IO server wrapping the FastAPI app — mounting
# `main:app` instead would serve the REST API but drop every websocket.
CMD ["uvicorn", "main:asgi_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
