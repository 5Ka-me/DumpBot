# syntax=docker/dockerfile:1
# Local Whisper for Dump Bot: faster-whisper (CTranslate2, CPU, int8) behind a small aiohttp API on port 9000.
# Multi-arch (linux/amd64, linux/arm64): all dependencies are binary wheels.
#   docker compose --profile whisper up -d --build        # as a part of the bot's stack (see README.md)
#   docker build -t dumpbot-whisper:local whisper/        # standalone

ARG PYTHON_IMAGE=python:3.12-slim-trixie
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="dumpbot-whisper" \
      org.opencontainers.image.description="Local speech-to-text for Dump Bot (faster-whisper, CPU)"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    WHISPER_MODELS_DIR=/models

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir /models && chown app:app /models

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-compile --root-user-action=ignore -r requirements.txt
COPY app.py ./

# /models is the model cache: compose mounts the named volume whisper-models here (a new volume starts with this
# directory's owner, app). Without a volume the model is downloaded again for every new container.
USER app
EXPOSE 9000
CMD ["python", "app.py"]
