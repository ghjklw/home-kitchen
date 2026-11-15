FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
# Proxy addresses are read from the real socket peer, never X-Forwarded-For.
CMD ["uvicorn", "home_kitchen.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
