# Quickstart

Get from zero to your first chat in under five minutes.

## Prerequisites

**LM Studio** must be running with at least one model loaded. Download it from [lmstudio.ai](https://lmstudio.ai) if you haven't already. LM Chat talks to LM Studio over HTTP — the two can run on the same machine or on different machines on your network.

If you want memory and document search to work, also load an embedding model in LM Studio. `text-embedding-nomic-embed-text-v1.5` ships with every LM Studio install and works well.

**Cloud-only setups** (OpenAI, OpenRouter, Groq, or any OpenAI-compatible endpoint) do not require LM Studio at all. You can configure cloud providers later in Settings after the first run.

### For Docker (no extra tools needed)

- Docker Engine 24+ or Docker Desktop

### For running from source

- Python 3.11 or later
- [`uv`](https://github.com/astral-sh/uv) (Python package manager)
- Node.js 20+ and `pnpm`

---

## Run with Docker

Docker is the fastest path. The container builds and runs natively on your platform — amd64 or arm64, so Apple Silicon, Raspberry Pi, and AWS Graviton all work.

**Step 1 — generate a secret key.**

LM Chat requires a signing key for sessions. Generate one and set it as an environment variable before starting the container:

```bash
export LM_CHAT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
```

To persist it across shell sessions, add the line to your shell profile or create a `deploy/.env` file:

```
LM_CHAT_SECRET=your-generated-value-here
```

**Step 2 — start the container.**

```bash
docker compose -f deploy/docker-compose.yml up -d
```

LM Chat is now running on port **8000**. Open [http://localhost:8000](http://localhost:8000) in your browser.

> **Docker + LM Studio on the same machine:** the compose file pre-configures `LM_STUDIO_BASE_URL=http://host.docker.internal:1234` so LM Chat can reach LM Studio on your host. No extra configuration needed.

---

## Run from source

Open two terminals.

**Terminal 1 — backend:**

```bash
git clone https://github.com/Chevron7Locked/lm-chat.git
cd lm-chat
cp .env.local.example .env.local
```

Open `.env.local` and set `LM_CHAT_SECRET` to a generated value:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the output in place of `REPLACE-WITH-GENERATED-SECRET`. Then start the server:

```bash
uv run uvicorn lmchat.app:app --port 8011 --reload
```

**Terminal 2 — web dev server:**

```bash
cd web
pnpm install
pnpm dev
```

The dev server proxies `/api` requests to the backend automatically. Open [http://localhost:5173](http://localhost:5173) in your browser.

---

## First run

### Create your account

LM Chat opens on a registration page. The very first person to register on a fresh database becomes the admin automatically — no separate admin setup step. Fill in a username and password and continue.

Subsequent users who register become non-admin by default. An admin can promote them later via Settings → Users.

### Connect to LM Studio

After registering, LM Chat walks you through the **Connect LM Studio** screen.

1. **Base URL** — enter your LM Studio address. The default is `http://localhost:1234`. If you are running LM Chat in Docker, use `http://host.docker.internal:1234` instead of `localhost`.
2. **API key** — leave blank unless you have enabled authentication in LM Studio's Developer settings.
3. Click **Test connection**. LM Chat probes LM Studio and shows how many models it can reach. Save and Skip are both locked until the probe passes.
4. **Default chat model** — pick any loaded model from the dropdown. This becomes the default for new chats. You can change it per chat later.
5. Click **Save and continue**.

> This screen verifies your LM Studio connection — test it, then **Save** or **Skip**. Cloud providers (OpenAI, OpenRouter, Groq) are configured afterward in **Settings → Providers**, once you reach the main interface.

---

## Your first chat

You land on the main chat interface. Click in the composer at the bottom, type a message, and press Enter (or the send button).

LM Chat streams the reply token by token. Each reply is labeled with the name of the active persona (for example, "General") so you always know which mode the conversation is in, not just which model.

The default model for the chat is whatever you set during setup. You can switch models or personas at any time using the controls at the top of the chat.

---

## What stays on your machine

All conversation history, documents, memory, and settings live in a local database (`lmchat.db` by default). LM Chat collects no telemetry. When you use LM Studio for inference, nothing leaves your machine. When you configure a cloud provider, prompts for that provider travel to that provider — the same as using any cloud API directly.

---

## Next steps

- [Overview](01-overview.md) — architecture tour, feature map, and key concepts
- [Providers and models](04-providers-and-models.md) — add OpenAI, OpenRouter, Groq, or a custom OpenAI-compatible endpoint
- [Personas and modes](03-personas-and-modes.md) — built-in personas, sub-agent slash commands, and per-chat model routing
