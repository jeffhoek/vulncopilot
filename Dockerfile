FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen

FROM python:3.12-slim
WORKDIR /app
RUN groupadd -r -g 10001 appuser && useradd -r -u 10001 -g appuser appuser
COPY --from=builder --chown=10001:10001 /app /app
RUN chown 10001:10001 /app
USER 10001
EXPOSE 8080
CMD ["/app/.venv/bin/chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8080"]
