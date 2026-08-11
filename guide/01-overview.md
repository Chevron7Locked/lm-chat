# Overview

LM Chat is the browser-native chat app for **LM Studio** — and now for any OpenAI-compatible cloud provider too. Run it on your own machine, open it from any browser on your network, and talk to your local models with persistent conversations, projects, memory, document search, and real tool use. No desktop app open, no data leaving your hardware unless you choose a cloud model.

This page explains what LM Chat is, the ideas it's built around, and how the interface is laid out. If you'd rather just get it running, jump to the [Quickstart](00-quickstart.md).

## What LM Chat is

LM Studio is the easiest way to run large language models locally. LM Chat is the chat client it should ship with: a fast, full-featured web app that connects to your LM Studio instance over HTTP and adds everything a serious daily-driver chat needs.

The LM Studio connection uses LM Studio's **native** API (`/api/v1/chat`), not the OpenAI compatibility layer that most third-party clients settle for. That native path is what unlocks real MCP tool execution, server-managed response chaining (so long conversations stay fast), live reasoning-event streaming, and per-model capability detection. See [Providers and models](04-providers-and-models.md) for the details.

On top of the local-first foundation, LM Chat also speaks to **cloud providers** — OpenAI, OpenRouter, Groq, or any OpenAI-compatible endpoint. You configure them in-app, and local and cloud models merge into one picker. Each chat routes to whichever provider it's set to use. Local stays the default; cloud is there when you want it.

LM Chat is local-first and self-hosted by design. There's no telemetry and no third-party analytics. Your chats, projects, documents, and memory live in a local database on your machine.

## The big ideas

### Local models, plus cloud

Your primary backend is LM Studio, reached over plain HTTP. LM Chat never reads LM Studio's config files, never shells out to a CLI, and never assumes the two run on the same host — loopback, LAN, Tailscale, or a tunnel all use the same code path. When you want a frontier cloud model, add an OpenAI-compatible provider in Settings and it joins the same model picker. See [Providers and models](04-providers-and-models.md).

### Personas vs sub-agent modes

This is the distinction worth learning first, because the two look similar but do very different things.

A **persona** is the chat's persistent system prompt. You set it from the chat-settings rail picker, which is the single source of truth for the active preset. The persona stays in effect for every turn in that conversation, and every reply is labeled with the persona's name (for example "Research") in place of the raw model id. There's no composer pill — the persona is simply the standing character of the chat.

A **sub-agent mode** is transient. Typing `/research`, `/code`, `/write`, `/analyze`, or `/architect` runs a single exchange in a clean, isolated context, shows a "…mode" pill on the composer, and injects a short summary back into the conversation when it finishes. It never changes the chat's persistent persona.

Six built-in presets back both mechanisms — **general**, **research**, **coder**, **creative**, **analyst**, and **architect** — and a **None** option sends the raw model prompt with no system context at all. Full walkthrough in [Personas and modes](03-personas-and-modes.md).

### Projects

A project bundles a system prompt, a folder of attached documents, and a collection of chats. New chats created inside a project inherit its instructions and can search its documents automatically. Projects are how context carries across many conversations instead of living in just one. See [Projects](06-projects.md).

### MCP tools

LM Chat gives models real tools through the Model Context Protocol. Your LM Studio MCP servers appear in the conversation automatically. Beyond those, a built-in MCP Store installs curated servers — GitHub, Filesystem, Fetch, web search, and more — with one click, and LM Chat's own MCP host runs them directly. That host is what lets **cloud** models call tools too, through an agentic tool loop. See [MCP and tools](05-mcp-and-tools.md).

### Documents, RAG, and memory

Upload documents to a project or a single chat and LM Chat chunks, embeds, and indexes them for hybrid full-text-plus-vector retrieval. Relevant passages are pulled into context as you chat, with the retrieval mode shown as a badge. Separately, an adaptive memory feature extracts durable insights from your conversations in the background — embedding-versioned, fully under your control to view, edit, or turn off. Both run on SQLite plus the embedding model LM Studio already serves, with no external vector store.

### Privacy and local-first

By default, nothing leaves your machine. Inference runs against your local LM Studio models, documents and memory are stored locally, and there is no telemetry. Cloud providers are strictly opt-in and only ever see the chats you explicitly route to them.

## How it's organized

The interface is built around three regions.

**The sidebar** lists your **projects** and your recent **chats**. Projects each have their own section that expands to show the chats inside them; standalone chats live below. On mobile this becomes a slide-out drawer.

**The composer** is the input column at the bottom of a chat. Type a message and send, or start with a `/` to open the slash-command palette — sub-agent modes (`/research`, `/code`, `/write`, `/analyze`, `/architect`), plus utilities like `/clear`, `/memory`, `/compact`, `/fork`, `/prompt`, and `/help`. The palette is keyboard-driven: Tab autocompletes, Enter dispatches.

**The chat-settings rail** is where you tune the current conversation — most importantly the **persona picker** that sets the chat's persistent system prompt, alongside controls for memory, documents, retrieval, and tools.

**Settings pages** hold everything global: the LM Studio connection, cloud providers, the MCP Store, per-preset model routing, and account and admin controls. LM Chat ships with a light and a dark theme (dark is the default) and works as a responsive web app on phones, where it can also be installed to the home screen as a PWA.

## Where to go next

- [Quickstart](00-quickstart.md) — get LM Chat connected to LM Studio and send your first message.
- [Personas and modes](03-personas-and-modes.md) — the persistent persona vs the transient sub-agent modes, in depth.
- [Providers and models](04-providers-and-models.md) — LM Studio's native path and adding cloud providers.
- [MCP and tools](05-mcp-and-tools.md) — the MCP Store, LM Studio servers, and tool use for local and cloud models.
- [Projects](06-projects.md) — knowledge bases, custom instructions, and inherited chats.
