# syntax=docker/dockerfile:1
# Dump Bot image: multi-stage uv build, slim non-root runtime. Multi-arch (linux/amd64, linux/arm64):
# every dependency ships binary wheels for both, so the runtime needs no compilers.
# The Mini App frontend (webapp/) is built with Node in its own stage and copied in as static files.
#   docker compose build                                  # this machine's architecture
#   docker buildx build --platform linux/arm64 -t dumpbot:local .   # e.g. for a Raspberry Pi

ARG PYTHON_IMAGE=python:3.12-slim-trixie
ARG UV_VERSION=0.12.23
ARG NODE_IMAGE=node:lts-slim

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------- webapp: the Mini App (React + Vite) -> /webapp/dist ----------
# Static files are the same for every architecture, so this stage runs natively even in a cross-build.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS webapp
WORKDIR /webapp
ENV npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false
# Dependencies first: this layer is reused until package.json / package-lock.json change.
COPY webapp/package.json webapp/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci
COPY webapp/ ./
RUN npm run build

# ---------- build: resolve the locked dependencies into /app/.venv ----------
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
# The long bytecode-compile timeout matters only for cross-builds under QEMU (Python starts very slowly there).
ENV UV_COMPILE_BYTECODE=1 \
    UV_COMPILE_BYTECODE_TIMEOUT=900 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first: this layer is reused until pyproject.toml / uv.lock change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Then the project itself, installed as a regular (non-editable) package: src/ is not needed at runtime.
# Building the wheel does not import dumpbot.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------- runtime ----------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="dumpbot" \
      org.opencontainers.image.description="Personal Telegram dump bot (aiogram + chutes.ai + PostgreSQL)"

# ffmpeg: optional OGG/Opus -> WAV conversion for speech-to-text (STT_CONVERT_WAV=true).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
# The Mini App build, served by the bot from WEBAPP_STATIC_DIR (default /app/webapp).
COPY --from=webapp /webapp/dist ./webapp

ENV PATH="/app/.venv/bin:${PATH}" \
    TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
CMD ["python", "-m", "dumpbot"]
