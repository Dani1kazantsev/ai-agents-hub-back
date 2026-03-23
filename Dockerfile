# Stage 1: Python dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
COPY mcp-servers/requirements.txt mcp-requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt -r mcp-requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

# Install Node.js 22 (required for Claude CLI)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude CLI globally
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Python packages
COPY --from=builder /install /usr/local

# Application code
COPY . .

# Create directory for per-user Claude configs
RUN mkdir -p /data/claude-configs

ENV CLAUDE_CONFIGS_DIR=/data/claude-configs
ENV MCP_SERVERS_DIR=/app/mcp-servers

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
