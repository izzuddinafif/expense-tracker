FROM ghcr.io/astral-sh/uv:0.12.0 AS uv

FROM python:3.12-slim

WORKDIR /app

# Install exactly the dependency graph captured in uv.lock.
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
ENV UV_NO_DEV=1 \
    UV_LOCKED=1 \
    PATH="/app/.venv/bin:$PATH"
RUN uv sync --no-install-project

# Copy application code
COPY . .

# Application files and the locked virtual environment are read-only at
# runtime. The entrypoint grants the unprivileged process access only to data/.
RUN adduser --disabled-password --gecos '' appuser

# Entrypoint handles runtime permission fix on /app/data volume
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER root
ENTRYPOINT ["/entrypoint.sh"]

# Healthcheck — verify bot process is actually running
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python healthcheck.py
