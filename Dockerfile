FROM python:3.12-slim

LABEL org.opencontainers.image.title="lm-chat" \
      org.opencontainers.image.description="Deeply-integrated chat UI for LM Studio" \
      org.opencontainers.image.source="https://github.com/Chevron7Locked/lm-chat" \
      org.opencontainers.image.licenses="AGPL-3.0"

# Non-root user for security
RUN groupadd -r lmchat && useradd -r -g lmchat -d /app -s /sbin/nologin lmchat

WORKDIR /app

# Copy only what's needed — no build step, no pip install
COPY server.py index.html manifest.json sw.js lm-chat-logo.svg ./

# Persistent data: DB, logs, signing key (mount a volume at /app/data)
RUN mkdir -p /app/data /app/data/logs && chown -R lmchat:lmchat /app

ENV PORT=3001 \
    LMSTUDIO_URL=http://host.docker.internal:1234 \
    LM_CHAT_AUTH=true \
    LM_CHAT_DB=/app/data/chats.db \
    LM_CHAT_LOGS=/app/data/logs \
    PYTHONUNBUFFERED=1

VOLUME /app/data

USER lmchat

EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:3001/api/health', timeout=3)" || exit 1

CMD ["python3", "server.py"]
