# TrainingPeaks MCP server - container image for hosted/remote deployment (e.g. Railway).
# Serves the MCP server over Streamable HTTP on $PORT at /mcp.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install build deps first (better layer caching), then the package.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

# Railway injects $PORT; default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# Start the HTTP transport. Auth is provided via the TP_AUTH_COOKIE env var.
CMD ["tp-mcp", "serve-http"]
