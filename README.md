# lm-chat

<p align="center">
  <img src="lm-chat-logo.svg" alt="lm-chat" width="120">
</p>

<p align="center">
  <strong>Your local models deserve a real frontend.</strong><br>
  Web access. Adaptive memory. Multi-user. Built on LM Studio's native API.
</p>

![lm-chat hero](docs/images/01-hero-chat-desktop.png)
*Main chat view — dark theme, desktop*

---

## What is this?

I use local LLMs for everything — brainstorming, planning, day-to-day questions, recommendations based on what I've already told it. The kind of stuff you'd use any AI assistant for, except it's running on my own hardware. LM Studio handles inference really well, but I kept hitting the same wall: no web access. I couldn't pick up a conversation from my phone, share the server with anyone else, or have it remember context across sessions without the desktop app open in front of me.

lm-chat fills that gap. It's a web frontend that handles everything around LM Studio — browser access from any device, persistent conversations that survive model swaps, adaptive memory that learns who you are, and multi-user auth so your whole household or team can share one server.

It's the only web client built on LM Studio's native API (`/api/v1/chat`), so you get MCP tools, server-managed conversation history, and model-aware features that aren't available through the OpenAI compatibility layer. No re-implementation, no compatibility hacks — just a tight integration with everything LM Studio already does well.

No `pip install`, no `npm`, no build step. Just run it.

### Docker (recommended)

```bash
docker run -d -p 3001:3001 -v ./lm-chat-data:/app/data \
  -e LMSTUDIO_URL=http://host.docker.internal:1234 \
  ghcr.io/chevron7locked/lm-chat:nightly
```

Multi-arch: `linux/amd64` + `linux/arm64` (Apple Silicon, Raspberry Pi).

### From source

```bash
git clone https://github.com/chevron7locked/lm-chat.git
cd lm-chat
python3 server.py
```

Open `http://localhost:3001`. Log in with the admin credentials printed to the console (see [First Run](#first-run) below).

**Requirements:** Python 3.10+ (or Docker) and LM Studio running with at least one model loaded.

### First Run

Authentication is **on by default**. On first launch, lm-chat creates an admin account and prints the credentials to stderr:

```
==================================================
  Admin account created
  Username: admin
  Password: <random-password>
  (set LM_CHAT_ADMIN_PASS to use your own)
==================================================
```

Copy the password from the terminal and log in at `http://localhost:3001`. You can change it in **Settings → Security** once logged in.

To set your own credentials upfront:

```bash
LM_CHAT_ADMIN_USER=myname LM_CHAT_ADMIN_PASS=mypassword python3 server.py
```

Or with Docker:

```bash
docker run -d -p 3001:3001 -v ./lm-chat-data:/app/data \
  -e LMSTUDIO_URL=http://host.docker.internal:1234 \
  -e LM_CHAT_ADMIN_USER=myname \
  -e LM_CHAT_ADMIN_PASS=mypassword \
  ghcr.io/chevron7locked/lm-chat:nightly
```

To disable auth entirely (single-user, trusted network): `LM_CHAT_AUTH=false`.

Once logged in as admin, you can invite other users from **Settings → Users**.

---

## Why the Native API?

Most third-party UIs talk to LM Studio through `/v1/chat/completions` — the OpenAI compatibility layer. lm-chat is built on `/api/v1/chat`, LM Studio's native endpoint. This matters because the native API exposes features the compatibility layer doesn't:

| Feature | Native API (`/api/v1/chat`) | OpenAI Compat (`/v1/chat/completions`) |
|---------|---------------------------|---------------------------------------|
| MCP tool execution | LM Studio runs your MCP servers | Not available |
| Response ID chaining | Server-managed history | Client resends everything |
| Reasoning events | Real SSE events | Parse `<think>` tags yourself |
| Capability detection | Vision, tool_use flags per model | Not available |
| Loaded instance routing | Use instance alias, avoid JIT reload | Not available |
| Model metadata | Context window, quantization, format | Basic only |

**Response ID chaining** is the big one. LM Studio manages the full conversation history server-side. lm-chat sends only the new message + a reference to the previous response. No token waste re-sending the entire history every turn.

LM Studio's desktop app uses all of this natively. lm-chat is the first web client that does too.

---

## Features

### Chat

- **SSE streaming** with live token stats (tokens/sec, time-to-first-token)
- **MCP tool execution** — all MCP servers configured in `~/.lmstudio/mcp.json` show up automatically and are on by default. Toggle per-conversation. Supports multi-step agentic loops
- **Native reasoning display** — thinking blocks from reasoning models (DeepSeek-R1, QwQ, Qwen3, etc.) in collapsible sections, with configurable depth (Off / Low / Medium / High)
- **Stop, edit, resend, regenerate** — full conversation control
- **Conversation forking** — branch from any message to explore alternatives
- **Auto-generated titles** via LLM
- **Suggested follow-ups** — optional follow-up questions after each response
- **Response feedback** — upvote / downvote individual responses; signals feed back into memory scoring

![MCP tool call](docs/images/02-mcp-tool-call-desktop.png)
*Live MCP tool call with streaming arguments — desktop*

### Quality Modes

Two opt-in inference modes that improve response quality at the cost of extra LLM calls. Toggle globally in Settings or per-conversation in the chat settings panel.

**Self-Consistency** — Generates 3 independent responses, then synthesizes the most consistent answer. Reduces noise on reasoning, factual, and technical questions. Skips synthesis when the first two responses are nearly identical (>80% token overlap). ~4× token cost.

**Chain of Verification** — Four-step pipeline: draft → extract verification questions → answer each question independently → synthesize a corrected response. Reduces hallucinations on factual claims by 50–70%. Based on [Dhuliawala et al., 2023](https://arxiv.org/abs/2309.11495). ~4× token cost.

Both can be enabled simultaneously: CoVe runs first, then SC synthesizes across CoVe's output.

### Conversation Organization

Pin your most-used chats, group related conversations into folders, and find anything instantly.

- **Pinned chats** — star any conversation to keep it at the top of the sidebar
- **Pinned messages** — pin individual assistant responses; they survive `/compact` and are searchable globally
- **Folders** — create named folders to organize chats by project, topic, or whatever makes sense
- **Collapsible sections** — folders collapse/expand with a click
- **Recent section** — everything else, sorted by last activity
- **Text search** — filter chats by title instantly
- **Semantic search** — press Enter to search by meaning across all messages (powered by the embedding model in LM Studio — `nomic-embed-text-v1.5` is included with every LM Studio install)

![Sidebar organization](docs/images/03-sidebar-organization-desktop.png)
*Sidebar with pinned chats, folders, and recent conversations — desktop*

### Agent Modes

Six system prompt presets, each tuned for a specific task. Switch from the settings panel or activate via slash commands:

| Command | Mode | Temperature |
|---------|------|------------|
| `/research` | Deep Research — multi-source synthesis | 0.4 |
| `/code` | Coding Agent — doc lookup, structured planning | 0.1 |
| `/write` | Creative Writing — craft-focused workshop | 0.9 |
| `/analyze` | Strategic Analyst — framework-driven analysis | 0.3 |
| `/architect` | Systems Architect — technical design | 0.2 |

Or choose **Custom** to write your own system prompt. Template variables are replaced on send: `{{current_date}}`, `{{day_of_week}}`, `{{current_time}}`, `{{model}}`, `{{memories}}`.

![Slash commands](docs/images/04-slash-command-menu-desktop.png)
*Slash command autocomplete — desktop*

### Share Conversations

Share any conversation as a read-only page. One click generates a unique URL — no login required to view.

- **Full markdown rendering** — code blocks, formatting, and structure preserved
- **Standalone pages** — minimal JavaScript, works anywhere
- **Strict CSP headers** — shared pages are sandboxed
- **Revocable** — delete the share anytime from the chat menu

![Share page](docs/images/05-share-page-desktop.png)
*Shared conversation — read-only page*

### Adaptive Memory

Your context follows you — across conversations, across model swaps. lm-chat builds a profile of your preferences, projects, skills, and opinions without you lifting a finger.

- **Auto-distillation** — insights extracted from conversations in the background
- **Bayesian scoring** — feedback on responses propagates back to the memory insights that shaped them
- **Cognitive decay** — stale memories fade naturally (freshness × usage × feedback scoring)
- **Category weighting** — identity and skill stay; session-specific details drift
- **Full user control** — view, edit, delete, toggle on/off, refine (LLM-based dedup/merge)
- **Zero external dependencies** — SQLite-backed, no vector store

![Memory panel](docs/images/06-memory-panel-desktop.png)
*Memory panel — categorized insights with decay indicators*

### Context Management

- **Context gauge** — live visualization of context window usage, click to compact
- **`/compact`** — LLM-summarized context when you need to free up space (pinned messages are preserved)
- **Instruction sandwich** — core instructions reinforced at end of system prompt for better adherence with local models

### MCP Tools

Your LM Studio MCP servers show up automatically — configured in `~/.lmstudio/mcp.json`, enabled by default in the UI. Toggle any server per-conversation.

**Remote MCP** — Connect additional MCP endpoints by URL with optional auth headers. Per-server credentials are stored server-side and never sent to the browser.

### Model Management

- **Hot model switching** — topbar dropdown or input pill
- **Capability badges** — Vision and Tool Use auto-detected per model from LM Studio metadata
- **Loaded / Idle status** — loaded models shown first, context window from the live instance config
- **Full sampling control** — temperature, top_p, top_k, min_p, repeat_penalty, max output tokens
- **Reasoning depth** — Off / Low / Medium / High for supported thinking models
- **Instance-aware routing** — uses the model's instance identifier (nickname) to avoid JIT reloads on every request
- **Connection monitoring** — live status indicator with health polling

![Model switching](docs/images/07-model-switching-desktop.png)
*Model switching with capability badges — desktop*

### Settings

Two settings surfaces:

**Full-page settings** (gear icon) — global defaults and account settings:

| Tab | Contents |
|-----|---------|
| **Chat** | System prompt presets, reasoning depth, suggested follow-ups, Self-Consistency, Chain of Verification, delete all chats |
| **Memory** | Toggle, view, edit, add, refine, clear insights |
| **Starters** | Customize welcome screen shortcuts |
| **Server** | LM Studio URL, API key, loaded models, MCP toggles, remote MCP endpoints, debug logging |
| **Profile** | Display name, change password |
| **Security** | TOTP 2FA setup and management |
| **Users** | Admin-only user management and invites |

**Per-chat settings panel** (right panel, per-conversation overrides):
- System prompt and preset (primary)
- Temperature (always visible)
- Advanced settings expander: top_p, top_k, min_p, repeat_penalty, max output tokens, reasoning depth
- Quality checks: Self-Consistency, Chain of Verification toggles

Per-chat settings override global defaults. Advanced sampling params default to LM Studio's instance config when not set.

![Settings panel](docs/images/08-settings-panel-desktop.png)
*Unified settings — tabbed navigation*

### Multi-User Auth

Optional (`LM_CHAT_AUTH=true`, enabled by default). Not bolted on — designed in from day one:

- **Invite-only accounts** with admin management
- **TOTP 2FA** — QR enrollment, works with any authenticator app (RFC 6238, stdlib-only QR generator)
- **Per-user API keys** — each user stores their own LM Studio auth token server-side
- **Per-user data isolation** — users only see their own conversations and memories
- **Scrypt password hashing** with timing-safe comparison
- **HttpOnly session cookies** with SameSite=Strict and sliding 30-day expiry
- **CSRF protection** via custom header validation
- **Rate limiting** on login (5 attempts per 15 minutes per IP)
- **Strict Content Security Policy** on all pages

### Debug Logging

Toggleable in Server Settings without restart. When enabled:

- Logs all requests, SSE events, memory operations, and tool calls
- Rotating log files (5 MB × 5 files = 25 MB max)
- View log file sizes directly in the settings panel

### Everything Else

- **Export** as Markdown or JSON
- **Keyboard shortcuts** — `Cmd+N` new chat, `Cmd+Shift+S` sidebar, `Cmd+,` settings, `Cmd+Shift+E` export, `Esc` close
- **PWA** — install on any device's home screen
- **Dark theme** — tuned for extended use, matched to LM Studio's aesthetic
- **Incognito mode** — toggle disables history and memory for the session (ephemeral, not persisted)
- **Accessibility** — full keyboard navigation, focus indicators, ARIA labels, screen reader support, `prefers-reduced-motion` respected
- **Mobile-responsive** — collapsible sidebar, 44px touch targets, always-visible actions on touch
- **Image and file attachments** — drag-and-drop images (JPEG, PNG, WebP, GIF) and text files (code, markdown, CSV, JSON, etc.)
- **Syntax highlighting** — vendored highlight.js, no CDN dependency
- **Slash command autocomplete** — `/research`, `/code`, `/write`, `/analyze`, `/architect`, `/compact`, `/help`

### Mobile

![Mobile chat](docs/images/09-mobile-chat-iphone.png)
*Chat — iPhone PWA*

![Mobile sidebar](docs/images/10-mobile-sidebar-iphone.png)
*Sidebar with pinned chats and folders — iPhone PWA*

---

## What lm-chat adds to LM Studio

LM Studio is already great on the desktop. lm-chat extends it into a web-accessible, multi-user platform:

| | LM Studio Desktop | lm-chat |
|---|---|---|
| Chat with MCP tools | Yes | Yes (via native API) |
| Web / browser access | No | Yes |
| Mobile PWA | No | Yes |
| Multi-user auth | No | Yes |
| Adaptive memory | No | Yes |
| Persistent chat history | Session-based | SQLite-backed |
| Semantic search | No | Yes |
| Pinned chats & folders | No | Yes |
| Share conversations | No | Yes |
| System prompt presets | No | Yes |
| Self-Consistency / CoVe | No | Yes |
| Remote access (Tailscale, etc.) | Requires desktop | Browser-based |

---

## Architecture

```
browser  ──HTTP──>  server.py  ──HTTP──>  LM Studio
                    (port 3001)           (port 1234)
                    SQLite · Auth         MCP servers
                    Memory · Logging      Inference
```

- **`server.py`** — stdlib Python, zero dependencies. Proxies native API, persists chats, manages auth, indexes embeddings, handles memory distillation, structured logging. ~5.4k lines.
- **`qr.py`** — pure-Python QR code generator for TOTP enrollment. ~345 lines.
- **`index.html`** — HTML shell. ~685 lines.
- **`style.css`** — all CSS, organized with `@layer` and native nesting. ~3.7k lines.
- **`app.js`** — all client-side JS. ~7.5k lines.
- **`manifest.json` + `sw.js`** — PWA support.
- **`highlight.min.js` + `highlight.min.css`** — vendored syntax highlighting, no CDN.
- **`logs/`** — rotating debug logs (auto-created, gitignored).

No frameworks. No transpilation. No node_modules. No build step.

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3001` | Server port |
| `LMSTUDIO_URL` | `http://localhost:1234` | LM Studio API URL |
| `LMSTUDIO_TOKEN` | *(empty)* | Bearer token (also configurable per-user in UI) |
| `LM_CHAT_AUTH` | `true` | Authentication (`false` to disable) |
| `LM_CHAT_SECRET` | *(auto-generated)* | Signing key for sessions and TOTP |
| `LM_CHAT_ADMIN_USER` | `admin` | Initial admin username (first run only) |
| `LM_CHAT_ADMIN_PASS` | *(auto-generated)* | Initial admin password (printed to stderr if not set) |
| `LM_CHAT_DEBUG` | *(off)* | Start with debug logging enabled (also toggleable in UI) |
| `LM_CHAT_DB` | `./chats.db` | SQLite database path (Docker: `/app/data/chats.db`) |
| `LM_CHAT_LOGS` | `./logs` | Log directory path (Docker: `/app/data/logs`) |
| `LM_CHAT_HTTPS` | *(off)* | Secure cookie flag (also auto-detected via `X-Forwarded-Proto`) |
| `LM_CHAT_HSTS` | *(off)* | Opt-in HSTS header.  `true`/`on`/`1` → standard directive; `preload` → also include `preload` token.  Only emitted over HTTPS. |
| `LM_CHAT_HSTS_MAX_AGE` | `63072000` | HSTS max-age in seconds (2 years). |
| `LM_CHAT_SINGLE_SESSION` | *(off)* | When set, every fresh login revokes the user's other sessions atomically. |
| `LM_CHAT_SETUP_TOKEN` | *(off)* | When set, `/api/auth/setup` requires `body["setup_token"]` to match — closes the first-visitor-wins admin window on public URLs. |
| `LM_CHAT_SCRYPT_N` | `131072` | scrypt cost parameter (OWASP 2024 floor).  Lower for testing on slow hardware. |
| `LM_CHAT_SCRYPT_R` | `8` | scrypt block size. |
| `LM_CHAT_SCRYPT_P` | `1` | scrypt parallelization. |
| `LMSTUDIO_MCP_JSON` | `~/.lmstudio/mcp.json` | Path to LM Studio MCP config |

### Docker

```bash
# Quick start
docker run -d -p 3001:3001 -v ./lm-chat-data:/app/data ghcr.io/chevron7locked/lm-chat:nightly

# With Docker Compose
curl -O https://raw.githubusercontent.com/Chevron7Locked/lm-chat/main/docker-compose.yml
docker compose up -d

# Nightly builds (latest from main)
docker pull ghcr.io/chevron7locked/lm-chat:nightly
```

**Platforms:** `linux/amd64`, `linux/arm64` (Apple Silicon, Raspberry Pi, AWS Graviton)

**Data persistence:** Mount a directory to `/app/data` — stores the SQLite database, logs, and signing key. Without a mount, data is lost on container restart.

**Security hardening:** The default `docker-compose.yml` runs with `read_only: true`, `no-new-privileges`, and all capabilities dropped. Only `/tmp` and `/app/data` are writable.

**Connecting to LM Studio:**
- **Same machine (Docker Desktop):** `LMSTUDIO_URL=http://host.docker.internal:1234` (default in image)
- **Remote server:** `LMSTUDIO_URL=http://192.168.1.x:1234`
- **Docker network:** `LMSTUDIO_URL=http://lmstudio:1234`

### LM Studio Setup

1. Load a model in LM Studio
2. Configure MCP servers in `~/.lmstudio/mcp.json` ([docs](https://lmstudio.ai/docs/app/mcp))
3. Enable **"Allow calling servers from mcp.json"** in LM Studio Developer Settings
4. For remote MCP: enable **"Allow per-request MCPs"** in Developer Settings
5. For semantic search: load an embedding model — `nomic-embed-text-v1.5` is bundled with LM Studio

### Run on Boot (macOS)

With Docker Compose and `restart: unless-stopped`, the container starts automatically when Docker Desktop launches. Enable **"Start Docker Desktop when you sign in"** in Docker Desktop settings.

For bare Python (without Docker):

```bash
cat > ~/Library/LaunchAgents/com.lm-chat.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lm-chat</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/lm-chat/server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>/path/to/lm-chat</string>
</dict>
</plist>
EOF

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lm-chat.plist
```

**Note:** If switching from launchd to Docker, unload the agent first:
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.lm-chat.plist
```

### Access from Phone

[Tailscale](https://tailscale.com) + `http://your-mac-hostname:3001`. Add to home screen for the full PWA experience.

---

## License

Copyright (c) 2026 chevron7locked

[GNU Affero General Public License v3.0](LICENSE)

For commercial licensing, contact dev@chevron7.io
