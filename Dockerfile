FROM python:3.12-slim

WORKDIR /app

# System deps for httpx
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY alarm_mcp ./alarm_mcp

RUN pip install --no-cache-dir -e .

# Persist alarm state across deploys (Fly volume mounts here)
ENV ALARM_MCP_STATE_DIR=/data
ENV ALARM_MCP_TRANSPORT=http
ENV ALARM_MCP_HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["alarm-mcp"]
