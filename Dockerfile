# The Claude Code CLI is a self-contained native binary — Node is only needed to FETCH it
# (npm gives us pinnable versions). So we grab it in a builder stage and ship a Node-free,
# slim Python runtime. Runs as a NON-root user: Claude Code refuses --dangerously-skip-permissions
# as root, and that flag is required to run MCP tools headless.

# ---- Stage 1: fetch the CLI binary via npm (pin with --build-arg CLAUDE_VERSION=2.1.181) ----
FROM node:22-bookworm-slim AS cli
ARG CLAUDE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION}
# The npm package ships a self-contained native ELF; extract just that (drops Node + npm + JS).
RUN cp "$(readlink -f "$(command -v claude)")" /claude && /claude --version

# ---- Stage 2: slim glibc Python runtime, no Node ----
# Debian slim (glibc), NOT alpine — the CLI is a glibc-linked ELF and would break on musl.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=cli /claude /usr/local/bin/claude

# Python deps in an isolated venv (avoids PEP 668 / system-pip issues).
ENV VENV=/opt/venv
RUN python3 -m venv "$VENV"
ENV PATH="$VENV/bin:$PATH"
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
WORKDIR /app

# Non-root user. Pre-create the CLI config dir owned by that user so a fresh named volume
# mounted there inherits non-root ownership (writable login).
RUN useradd -m -u 1000 app && mkdir -p /home/app/.claude \
 && chown -R app:app /app /home/app/.claude
USER app

ENV HOST=0.0.0.0 \
    PORT=8000 \
    CLAUDE_CONFIG_DIR=/home/app/.claude
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
