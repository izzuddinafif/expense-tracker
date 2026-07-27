FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN adduser --disabled-password --gecos '' appuser \
    && chown -R appuser:appuser /app

# Entrypoint handles runtime permission fix on /app/data volume
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER root
ENTRYPOINT ["/entrypoint.sh"]

# Healthcheck — verify bot process is actually running
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python healthcheck.py
