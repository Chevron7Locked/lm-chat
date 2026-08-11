# Glossary

Plain-language definitions for the terms you meet across LM Chat. Where a term has a page of its own, the entry links to it. Terms are listed alphabetically.

A quick note on one pair worth getting right from the start: a **persona** is your chat's lasting personality (a system prompt you set in the rail), while a **sub-agent mode** is a one-off helper you launch with a slash command. They share names on purpose, but they are not the same thing — see both entries below, and [Personas and modes](03-personas-and-modes.md).

### A/B compare

A way to send the same message to two models at once and see their answers side by side. You turn it on in the chat's settings, pick Model A and Model B, and your next message streams into both panes in parallel; clicking **Use this response** on the one you prefer commits it as the turn's reply and switches A/B compare back off. See [Productivity](08-productivity.md).

### Admin

The single person who owns the shared setup for an LM Chat install — model connections, providers, the MCP Store, the user roster, and quotas. The first account created on a fresh install becomes the admin automatically; everyone else is a regular member who uses what the admin has configured. See [Settings and admin](10-settings-and-admin.md).

### Agentic loop

The cycle LM Chat's own MCP host runs so a model can actually use tools: call a tool, read the result, decide what to do next, and repeat until the model is finished. This loop is what lets cloud models use tools, since they can't reach LM Studio's server-side tools directly. See [MCP and tools](05-mcp-and-tools.md).

### Anthropic

Anthropic is **not** a built-in provider in LM Chat. To talk to an Anthropic model, reach it through an OpenAI-compatible gateway (such as OpenRouter) added as a provider. See [Providers and models](04-providers-and-models.md).

### Chain-of-verification

A quality mode in which the model checks its own work before you see it: it drafts an answer, writes verification questions about the load-bearing claims, answers those questions separately, then revises the draft in light of what the checks turned up. It trades extra time and tokens for fewer confident-but-wrong details. See [Productivity](08-productivity.md).

### Chunk

One of the overlapping passages a document is split into when you upload it — roughly 500 tokens each, with a small overlap so a sentence spanning a boundary isn't lost. Each chunk is embedded and indexed so retrieval can pull just the relevant pieces of a file into a reply. See [Knowledge and memory](07-knowledge-and-memory.md).

### Compaction

What `/compact` does: summarizes the oldest span of a long chat with a local model and archives it into a collapsed, expandable folded recall tab, rather than deleting anything. Frees up context while keeping the summary in play. See [Chatting](02-chatting.md).

### Context window

The amount of text a model can hold in mind at once for a single turn — your messages, its replies, any retrieved passages, and pinned insights all count against it. A meter above the composer shows roughly how much of the loaded model's context window you've used and turns amber as you near the limit.

### Default model

The model new chats start with. By default it's the first model loaded in LM Studio, so LM Chat just works without you pinning anything; you can set a specific default in Settings. Changing the default affects new chats going forward, not chats that already have their own model. See [Providers and models](04-providers-and-models.md) and **sticky model**.

### Document

A file you upload — plain text, Markdown, HTML, or PDF — that LM Chat reads, splits into chunks, and indexes so its contents can be retrieved into your chats. A document is either loose (available across your chats) or attached to a project (scoped to that project's chats). See [Knowledge and memory](07-knowledge-and-memory.md).

### Embedding / embedding model

An embedding turns a piece of text into a vector — a list of numbers that captures its meaning — so passages can be matched by intent, not just by exact words. The embedding model is the model that produces those vectors; it runs in LM Studio and powers the meaning-based half of retrieval and recall. If no embedding model is loaded, vector search goes quiet and only keyword matching remains. See [Knowledge and memory](07-knowledge-and-memory.md).

### Folder

A label in the sidebar for grouping your loose, everyday chats. A folder is private to your account; you drag chats into it and reorder them, and the arrangement sticks. Folders are separate from projects, which are a richer space with their own chats, documents, and instructions. See [Organizing and sharing](09-organizing-and-sharing.md).

### Full-text search

The keyword half of retrieval — matching the exact terms in your question against an index of your document chunks and messages. LM Chat blends it with vector (meaning-based) search so a query catches both passages that share your wording and passages that share your intent. See [Knowledge and memory](07-knowledge-and-memory.md).

### Incognito chat

A conversation you intend to be temporary. It auto-expires after a set lifetime (one hour by default), skips long-term memory writes, and can't be turned into a share link. It is a local-retention control, not an anonymity feature — the chat still runs through your configured provider like any other. See [Organizing and sharing](09-organizing-and-sharing.md).

### LM Studio

The local app that runs large language models on your own machine, and LM Chat's primary backend. LM Chat connects to it over plain HTTP using LM Studio's native API, which unlocks real tool execution, response chaining, live reasoning streaming, and per-model capability detection. See [Providers and models](04-providers-and-models.md).

### MCP (Model Context Protocol)

The standard LM Chat uses to give models real abilities beyond text — searching the web, reading files, querying GitHub, and more. Abilities arrive as tools, and tools come from MCP servers. See [MCP and tools](05-mcp-and-tools.md).

### MCP server

A small program that speaks the Model Context Protocol and offers one or more tools. LM Studio's own MCP servers appear in your chats automatically; servers you install through the MCP Store are run by LM Chat's own host. You can enable or disable a whole server at any time. See [MCP and tools](05-mcp-and-tools.md).

### MCP Store

A curated catalog of well-known MCP servers — GitHub, Filesystem, Fetch, Playwright, Wolfram Alpha, and others — that you can install in a click from Settings. Servers that talk to an outside service ask for their API key or token during install; secrets are stored server-side, encrypted, and never shown back to you. See [MCP and tools](05-mcp-and-tools.md).

### MCP tool

A single ability offered by an MCP server — for example, "fetch a web page" or "create a GitHub issue." You can allow or deny individual tools within a server, so you can keep a server you mostly trust while ruling out one capability. See [MCP and tools](05-mcp-and-tools.md).

### Memory

The system that lets a conversation build on what came before. It has two parts: **pinned insights** you choose to keep, and **recall** of your earlier messages. Memory is stored locally, under your control to view, edit, or turn off. See [Knowledge and memory](07-knowledge-and-memory.md).

### Model

A specific large language model you talk to — local (served by LM Studio) or from a cloud provider. Local and cloud models merge into one picker, grouped by provider, and each chat routes to whichever model you pick for it. See [Providers and models](04-providers-and-models.md).

### OpenAI-compatible

A provider whose API follows the same shape as OpenAI's. LM Chat can connect to any OpenAI-compatible cloud endpoint — OpenAI, OpenRouter, Groq, or a custom one — so a wide range of cloud models work without bespoke integration. See [Providers and models](04-providers-and-models.md).

### Per-preset routing (preset models)

The setting that pins a specific provider and model to each persona, in **Settings → Preset Models**. It lets, say, Research run on a stronger model than your everyday default while Coder uses one tuned for code; a sub-agent mode uses the model pinned to its persona, falling back to the chat's current model if none is set. See [Personas and modes](03-personas-and-modes.md).

### Persona

Your chat's lasting personality — the system prompt that tells the model how to think and respond for every message in that chat. A persona is **persistent**: you set it once in the chat-settings rail picker (the single source of truth for the active preset), and it stays until you deliberately change it. LM Chat ships six personas — General, Coder, Creative, Research, Analyst, Architect — plus **None** (no system prompt at all). A persona is not a sub-agent mode: changing the persona reassigns who you're talking to for the whole conversation. See [Personas and modes](03-personas-and-modes.md).

### Pinned insight

A durable note — a preference, a fact, a working agreement — that you pin so it carries forward across conversations. Insights are injected into the model's context on every turn, whether or not retrieval is otherwise on. Pin one with the `/memory` command or from the Memory page. See **memory** and [Knowledge and memory](07-knowledge-and-memory.md).

### Pinned message

A bookmark inside a single chat that marks a turn worth returning to. Clicking its chip scrolls the message into view. It is purely a personal reading aid stored in your browser — nothing is copied, sent to the model, or included in a share link — and it is different from a pinned insight and from pinning a whole chat. See [Organizing and sharing](09-organizing-and-sharing.md).

### Preset

The underlying set of six built-in instruction profiles — general, research, coder, creative, analyst, architect — that backs both personas and sub-agent modes. When you set a chat's persona you're choosing a preset; when you run a mode you're invoking the same preset's instructions in a transient session. See [Personas and modes](03-personas-and-modes.md).

### Project

A space that bundles three things that belong together: custom instructions, a knowledge base of documents, and a collection of chats. Every chat you start inside a project inherits its instructions and can search its documents automatically — so a project is where context lives across many conversations. See [Projects](06-projects.md).

### Prompt library

Your saved, reusable prompts. Save a prompt once with a name in the **Prompts** page, then pull it into any chat with `/prompt <name>`, which drops its text into the composer for you to edit before sending. See [Productivity](08-productivity.md).

### Provider

A source of models. LM Chat talks to two kinds: your local LM Studio instance and any OpenAI-compatible cloud provider (OpenAI, OpenRouter, Groq, or a custom one) an admin adds. Local is the default; cloud is opt-in and only ever sees the chats you route to it. See [Providers and models](04-providers-and-models.md).

### Quality mode

An option that asks the model to work harder on a single turn for a more trustworthy answer, trading extra time and tokens for reliability. LM Chat offers two, as independent toggles in the chat-settings rail: **self-consistency** and **chain-of-verification**. See [Productivity](08-productivity.md).

### Quota

Each member's daily allowance of requests and tokens. Defaults are generous (100,000 tokens and 1,000 requests per day) and most installs never change them; you can see your own usage on the **Quota** tab, and an admin can set custom limits per user. Admins aren't blocked when they hit a limit. See [Settings and admin](10-settings-and-admin.md).

### RAG / retrieval

Retrieval — also called RAG, for retrieval-augmented generation — is how your own documents and past messages reach the model. Instead of pasting passages in by hand, LM Chat searches your indexed chunks for the ones most relevant to your message and folds them into the model's context for that turn. The search blends keyword and vector matching. See [Knowledge and memory](07-knowledge-and-memory.md).

### Reasoning model / thinking block

A reasoning model works through a problem step by step before answering, and LM Chat streams that thinking separately. You'll see a block labeled **Thinking…** while it reasons, switching to **Reasoning** once the answer begins; it's collapsed by default and you can expand it to read the chain of thought. A spell of "Thinking…" with no answer yet is normal, not a hang. See [Chatting](02-chatting.md).

### Recall

The part of memory that quietly indexes your messages as you chat, so a later question can surface a relevant earlier exchange. Recall rides the same per-chat RAG toggle as document retrieval, and messages in incognito chats are not indexed. See [Knowledge and memory](07-knowledge-and-memory.md).

### Response chaining

A speed optimization on LM Studio's native path: rather than resending the whole conversation each turn, LM Chat chains turns by reference on the server, so long conversations stay fast. Editing an earlier message invalidates the chain from that point so the model answers your revised wording. See [Providers and models](04-providers-and-models.md).

### Retrieval mode (inline / hybrid / focused)

Which of three strategies LM Chat uses to bring documents into a chat, shown as a badge near the composer. **Inline** keeps a small project corpus entirely in context; **hybrid** (the default) searches your chunks and pulls in only the top matches per message; **focused** pins the chat to a single document as its whole corpus. See [Knowledge and memory](07-knowledge-and-memory.md).

### Self-consistency

A quality mode in which the model drafts the same prompt several times independently, then returns the answer that sits most squarely in the middle of the pack. The idea is that a correct answer tends to recur across independent tries while one-off mistakes don't agree, so this filters out the flukes — at the cost of several generations instead of one. See [Productivity](08-productivity.md).

### Share link

A read-only web page minted from a normal chat — of the form `/share/<token>` — that you can send to anyone, no account needed. The link is the only thing granting access, so treat it like a password; revoke it from the same menu to switch access off. Incognito chats can't be shared. See [Organizing and sharing](09-organizing-and-sharing.md).

### Slash command

A command you type in the composer starting with `/`, chosen from the slash palette. There are two kinds: **sub-agent modes** (`/research`, `/code`, `/write`, `/analyze`, `/architect`) and utility commands (`/clear`, `/memory`, `/compact`, `/fork`, `/prompt`, `/help`). See [Chatting](02-chatting.md).

### Sticky model

Each chat keeps its own model. The first time you pick a model for a conversation, that choice sticks to that chat — switching models in one chat never affects another, and changing the global default later won't disturb a chat that already has its own model. See **default model** and [Providers and models](04-providers-and-models.md).

### Streaming

The way replies arrive token by token as the model writes them, with a soft caret at the leading edge, so you read along as the answer forms. A **Stop** button replaces Send during a stream; a stopped reply keeps whatever text arrived. See [Chatting](02-chatting.md).

### Sub-agent mode

A one-off helper you launch with a slash command (`/research`, `/code`, `/write`, `/analyze`, `/architect`) to get a single focused task done. It runs in a clean, isolated session that doesn't see your main chat history, shows a "…mode" pill while active, and hands a short summary back to your conversation when you finish. A sub-agent mode is **transient** and never changes your chat's persona — never conflate the two. See [Personas and modes](03-personas-and-modes.md).

### Sub-session

The clean, isolated workspace a sub-agent mode runs in. It starts fresh, sees only the task you give it (not the surrounding chat), and exists only for that single errand before returning its summary. See [Personas and modes](03-personas-and-modes.md).

### Temperature

A sampling setting that controls how freely a model wanders: lower temperatures make answers more conservative and predictable, higher ones more varied and creative. Each persona carries sensible defaults — a fact-checking persona answers conservatively, a creative one more freely — without you touching any sliders. See [Personas and modes](03-personas-and-modes.md).

### Token

The unit models read and write in — roughly a word-piece, smaller than a word. Tokens are how length is measured throughout LM Chat: the context window, chunk size, your daily quota, and stream speed (tokens per second) are all counted in tokens.

### Tool call

A single instance of a model invoking an MCP tool mid-conversation. It appears in the stream as a collapsible card showing the tool's friendly name and status; expand it to see the arguments the model sent and the result it got back. See [MCP and tools](05-mcp-and-tools.md).

### Vector

The list of numbers an embedding model produces to represent a piece of text's meaning. Storing chunks and messages as vectors is what lets retrieval match by intent rather than exact wording; vectors are stamped with the embedding model that built them, so switching models can require a re-embed. See [Knowledge and memory](07-knowledge-and-memory.md).

### Web search

A per-chat setting that lets a model reach the live internet before it answers, so its reply can draw on current information instead of only its training data. LM Chat runs the search behind the scenes — through a privacy-respecting provider such as SearXNG, falling back to DuckDuckGo — and folds the top results into the model's context. It's a lightweight "look it up first" step, distinct from the tool-driven search an MCP server offers. See [Productivity](08-productivity.md).

## See also

- [Personas and modes](03-personas-and-modes.md) — persona vs sub-agent mode, in depth
- [Providers and models](04-providers-and-models.md) — providers, models, and routing
- [MCP and tools](05-mcp-and-tools.md) — MCP servers, tools, and the Store
- [Knowledge and memory](07-knowledge-and-memory.md) — RAG, embeddings, chunks, and memory
- [Troubleshooting & FAQ](13-troubleshooting-faq.md) — when something isn't behaving
