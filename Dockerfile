FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
ARG INFERENCE=false
RUN if [ "$INFERENCE" = "true" ]; then uv sync --locked --no-dev --no-editable --extra inference; else uv sync --locked --no-dev --no-editable; fi

FROM python:3.12-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
ARG INFERENCE=false
RUN groupadd --gid 10001 modera && useradd --uid 10001 --gid modera --no-create-home modera
COPY --from=builder /app/.venv /app/.venv
USER 10001:10001
RUN if [ "$INFERENCE" = "true" ]; then python -c "import torch; import transformers"; fi
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"
CMD ["uvicorn", "modera.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "30", "--no-server-header", "--no-access-log", "--no-proxy-headers"]
