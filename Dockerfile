# Pinned by manifest-list digest so the build is reproducible and Trivy/SBOM
# tooling has something stable to reference.  Dependabot updates both the
# digest and the trailing tag comment together.
FROM python:3.14-slim@sha256:7a500125bc50693f2214e842a621440a1b1b9cbb2188f74ab045d29ed2ea5856

LABEL org.opencontainers.image.title="lm-chat" \
      org.opencontainers.image.description="Deeply-integrated chat UI for LM Studio" \
      org.opencontainers.image.source="https://github.com/Chevron7Locked/lm-chat" \
      org.opencontainers.image.licenses="AGPL-3.0"

# PYTHONDONTWRITEBYTECODE keeps the image immutable under read_only: true
# (no .pyc cache writes to attempt during runtime).

# Non-root user for security
RUN groupadd -r lmchat && useradd -r -g lmchat -d /app -s /sbin/nologin lmchat

WORKDIR /app

# Copy only what's needed — no build step, no pip install
COPY server.py qr.py index.html style.css app.js manifest.json sw.js lm-chat-logo.svg highlight.min.js highlight.min.css ./

# Persistent data: DB, logs, signing key (mount a volume at /app/data)
RUN mkdir -p /app/data /app/data/logs && chown -R lmchat:lmchat /app

ENV PORT=3001 \
    LMSTUDIO_URL=http://host.docker.internal:1234 \
    LMSTUDIO_MCP_JSON=/lmstudio/mcp.json \
    LM_CHAT_AUTH=true \
    LM_CHAT_DB=/app/data/chats.db \
    LM_CHAT_LOGS=/app/data/logs \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER lmchat

EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:3001/api/health', timeout=3)" || exit 1

CMD ["python3", "server.py"]
