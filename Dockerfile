# syntax=docker/dockerfile:1.7
# US-064: Multi-stage Padrino image. Stage 1 resolves dependencies via uv into a
# self-contained virtualenv; stage 2 ships a slim runtime with that venv plus
# the package source. The entrypoint is the ``padrino`` CLI so the same image
# serves the API (``serve``), the scheduler (``scheduler``), and one-shot
# bootstrap (``bootstrap``) by switching the command argv.

ARG PYTHON_VERSION=3.12

# ---------- Stage 1: builder ----------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /uvx /usr/local/bin/

WORKDIR /opt/padrino

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable

# ---------- Stage 2: runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/padrino/.venv/bin:${PATH}" \
    PADRINO_DB_URL="sqlite+aiosqlite:////var/lib/padrino/padrino.db" \
    PADRINO_LOG_LEVEL=INFO \
    PADRINO_API_HOST=0.0.0.0 \
    PADRINO_API_PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 padrino \
    && useradd  --system --uid 1000 --gid padrino --home /opt/padrino padrino \
    && install -d -o padrino -g padrino /var/lib/padrino

COPY --from=builder --chown=padrino:padrino /opt/padrino /opt/padrino

WORKDIR /opt/padrino
USER padrino:padrino

EXPOSE 8000

# Liveness probe — the API container's ``/healthz`` is the cheap check. The
# scheduler's readiness lives at ``/healthz/scheduler`` and is checked by the
# scheduler container in docker-compose.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=6 \
    CMD python -c "import sys, urllib.request as u; \
        sys.exit(0 if u.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

ENTRYPOINT ["padrino"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
