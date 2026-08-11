# How-tos

Short, step-by-step recipes for the things people do most in LM Chat. Each one is a goal plus a few steps grounded in the real interface, then a link to the page with the full story. If a recipe assumes you're already connected and chatting, start with [Quickstart](00-quickstart.md) first.

A note on who can do what: connecting model backends, adding providers, installing MCP servers, and managing people are admin tasks. In a single-admin install, the first person to register is the admin; everyone else uses what the admin has set up. Recipes that need admin rights say so.

## Connect models

### Connect to LM Studio

Goal: point LM Chat at your local LM Studio instance so its models appear in the picker.

1. Open **Settings → LM Studio**.
2. Set **Base URL** to where LM Studio is listening — `http://localhost:1234` on the same machine, or `http://host.docker.internal:1234` from inside Docker.
3. If you've turned on authentication in LM Studio, enter the **API key**; otherwise leave it blank.
4. Click **Test connection**. On success it reports how many models are reachable.
5. Pick a **Default model** from the dropdown, then **Save**.

Full detail: [Providers and models](04-providers-and-models.md)

### Add a cloud provider (admin)

Goal: route chats to OpenAI, OpenRouter, Groq, or any OpenAI-compatible endpoint.

1. Open **Settings → Providers** and click **Add provider**.
2. Pick a **Provider** preset (OpenRouter, Groq, OpenAI) to prefill the base URL, or choose **Custom** and supply your own slug and **Base URL**.
3. Enter the **API key** (stored securely, never shown back). Optionally set a **Default model**.
4. Click **Test connection** to probe the endpoint with your credentials.
5. Leave **Enabled** on and **Save**. It takes effect immediately, with no restart.

For an Anthropic model, reach it through an OpenAI-compatible gateway such as OpenRouter — native Anthropic is not a built-in provider.

Full detail: [Providers and models](04-providers-and-models.md)

### Curate which cloud models appear (admin)

Goal: limit a provider's models in the picker to the handful you actually use.

1. Open **Settings → Providers** and edit the provider.
2. Run **Test connection** — a successful test unlocks the searchable model checklist (the allowlist) below.
3. Leave the list **empty to allow all** of the provider's models, or **select specific models** to restrict the picker to just those.
4. Filter and select in bulk, then **Save**. An active allowlist shows a count badge on the provider row.

Full detail: [Providers and models](04-providers-and-models.md)

### Change the default model

Goal: choose which model new chats start with.

1. Open **Settings → LM Studio** (or **Settings → Chat**).
2. Pick a **Default model** from the picker. If a model isn't loaded yet, switch to manual entry and type its id.
3. **Save**. The change applies to **new** chats going forward; existing chats keep their own sticky model.

Full detail: [Providers and models](04-providers-and-models.md)

## Set personas and modes

### Set a chat's persona

Goal: give a chat a lasting personality that shapes every reply.

1. Open the chat and reveal its **chat-settings rail**.
2. At the top, open the **Preset** selector.
3. Choose a persona — General, Coder, Creative, Research, Analyst, Architect, or None.

Your choice saves to that chat immediately, applies to the next message and every one after, and survives a reload. The rail picker is the only control that sets the persona; each reply is labeled with the persona's name.

Full detail: [Personas and modes](03-personas-and-modes.md)

### Run a sub-agent mode

Goal: hand one focused task to a specialist without changing your chat's persona.

1. In the composer, type a slash command — `/research`, `/code`, `/write`, `/analyze`, or `/architect`. You can pass the request inline, for example `/research what changed in the latest release`.
2. A clean workspace opens with a "…mode" pill. Have your exchange there.
3. Press **Summarize → main chat**, then **Add to main chat →** to drop the result into your conversation.

The mode is transient — it runs, returns a summary, and leaves your chat's persona untouched.

Full detail: [Personas and modes](03-personas-and-modes.md)

### Pin a different model to a persona

Goal: run a persona (and its matching sub-agent mode) on a model you choose.

1. Open **Settings → Preset models**.
2. Find the persona you want to route, for example Research or Coder.
3. Pick the **provider and model** to pin to it, then save.

When a sub-agent mode launches, it uses the model pinned to that persona; if none is pinned, it falls back to the model the chat is currently using.

Full detail: [Personas and modes](03-personas-and-modes.md) and [Providers and models](04-providers-and-models.md)

## Add tools with MCP

### Install an MCP server from the Store

Goal: give models a real ability — web search, GitHub, a browser, exact math — in one click.

1. Open the **MCP Store** from Settings.
2. Browse the catalog (GitHub, Filesystem, Fetch, Playwright, Wolfram Alpha, and more) and click **Install** on the one you want.
3. If the server needs a secret — an API key, token, or URL — the install panel expands; fill in the required fields and finish the install.

Secrets are stored server-side, encrypted at rest, and never shown back.

Full detail: [MCP and tools](05-mcp-and-tools.md)

### Add a custom MCP server

Goal: install a server that isn't in the catalog — one you wrote or one not yet curated.

1. In the MCP Store, choose to add a custom server.
2. Give it a **name** and a **slug**, then pick a transport:
   - **stdio** — a local command LM Chat launches (such as an `npx` or `uvx` invocation), with its arguments.
   - **http** or **sse** — a server reachable at an `http(s)` URL.
3. Add any secrets the server needs, then install. From then on it behaves like a catalog server.

Full detail: [MCP and tools](05-mcp-and-tools.md)

### Turn tools on for a chat

Goal: offer specific tools to the model for this conversation.

1. Look at the **tools bar** above the composer — a row of pills, one per available tool source. (It only appears for models trained to use tools.)
2. Click a pill to switch that tool source on or off.
3. Send your message — only the tools you switched on are offered. Your selection sticks with the chat.

To rule out a single capability, deny that tool in its server's panel under the MCP settings.

Full detail: [MCP and tools](05-mcp-and-tools.md)

## Work with your documents

### Start a project and attach documents

Goal: set up a shared space where instructions and documents carry across many chats.

1. In the sidebar's **Projects** section, click **+ (New project)**, type a name, and press **Create**.
2. On the project page, open the **Documents** tab and **Upload** new files, or **Attach existing** documents.
3. Open the **Settings** tab to add **Custom instructions** for every chat in the project, then **Save settings**.

Full detail: [Projects](06-projects.md) and [Knowledge and memory](07-knowledge-and-memory.md)

### Ask questions over your documents (RAG)

Goal: get answers grounded in your files, without pasting anything in.

1. Upload documents on the **Documents** page, or attach them to a project. Each file is chunked, embedded, and indexed.
2. Make sure an **embedding model is loaded in LM Studio** (such as `text-embedding-nomic-embed-text-v1.5`) — vector retrieval depends on it.
3. Chat normally. Retrieval auto-enables when there are documents to search; LM Chat folds the most relevant passages into each turn.
4. Check the **retrieval badge** near the composer for the active mode (Inline, Hybrid, or Focused), or to toggle RAG for the chat.

Full detail: [Knowledge and memory](07-knowledge-and-memory.md)

### Pin an insight to memory

Goal: keep a preference or fact that the model carries into future chats.

1. In any chat, type `/memory` followed by the text — for example, `/memory I prefer concise answers with code first`.
2. Or open the **Memory** page from the sidebar and add an insight in the input at the top.
3. Manage insights on the **Memory** page: search, edit in place, unpin, or use **Refine** to merge duplicates and tighten the set.

Insights respect projects and reach the model every turn, whether or not retrieval is otherwise on.

Full detail: [Knowledge and memory](07-knowledge-and-memory.md)

## Get more from a chat

### Search the web in a chat

Goal: let a chat draw on current information instead of only its training data.

1. Turn on **web search** for the chat (it's a per-chat setting).
2. Send your message. Behind the scenes, LM Chat runs a search and folds the top results — titles, URLs, and snippets — into the model's context.
3. The model writes its reply with those results in hand, so it can ground its answer in what's online now.

Searches go through a privacy-respecting provider (SearXNG by default, with a DuckDuckGo fallback), configured during setup.

Full detail: [Productivity](08-productivity.md)

### Compare two models (A/B)

Goal: send the same prompt to two models and judge their answers side by side.

1. In the chat's settings, enable **A/B compare**.
2. Choose **Model A** and **Model B**.
3. Send your message. The two answers stream in parallel, each in its own labeled pane.
4. Click **Use this response** on the one you prefer. It's committed to the chat, and A/B compare switches back off.

Full detail: [Productivity](08-productivity.md)

### Free up context in a long chat

Goal: make room for new turns in a chat that's filling up the model's context window, without losing anything.

1. In the composer, type `/compact`.
2. LM Chat summarizes the oldest messages with a local model and archives them into a collapsed **folded recall tab** — nothing is deleted.
3. Expand the tab any time to read the original messages in full.

Full detail: [Chatting](02-chatting.md)

### Get a more reliable answer (quality modes)

Goal: trade extra time and tokens for a more trustworthy result on a single turn.

1. Open the chat-settings rail and find the **Quality** section.
2. Turn on **Self-consistency** to have the model draft several times and return the answer it converged on.
3. Or turn on **Chain-of-verification** to have the model draft, generate verification questions, answer them, and revise.
4. You can run either on its own. Expect the turn to take longer while a mode is active.

Full detail: [Productivity](08-productivity.md)

### Save and reuse a prompt

Goal: store instructions you keep rewriting and drop them into any chat.

1. Open **Prompts** from the sidebar.
2. Under **New prompt**, give it a unique **Name** (such as `summarize-code`), paste its **Content**, and click **Create prompt**.
3. In any chat, type `/prompt <name>` — for example, `/prompt summarize-code`. LM Chat replaces the command with that prompt's content, ready to add to or send.

Full detail: [Productivity](08-productivity.md)

## Organize and share

### Organize chats with folders

Goal: keep your chat library tidy with your own folders.

1. Use the new-folder control at the top of the chat list, type a name, and confirm.
2. Grab a chat row's **drag handle** and drag it into the folder, or use the row's context menu to **Move to folder**. Drag order is remembered per band.
3. To keep one chat handy, **Pin** it from the row's context menu — pinned chats lift to the top band.

The handle is keyboard-operable: Tab to it, Space or Enter to pick up, arrows to move, Space/Enter/Escape to drop.

Full detail: [Organizing and sharing](09-organizing-and-sharing.md)

### Share a conversation (read-only link)

Goal: publish a chat as a read-only web page anyone can open — no account needed.

1. Open the chat's top-bar menu and choose **Share**. LM Chat mints a `/share/<token>` link and copies it to your clipboard.
2. Send the link. The recipient sees a clean, read-only page of the conversation and cannot reply or sign in.
3. To stop sharing, open the same **Share** menu and **revoke** the link — the URL stops working from that moment.

The token is the only thing granting access, so treat the link like a password. Sharing is disabled for incognito chats.

Full detail: [Organizing and sharing](09-organizing-and-sharing.md)

### Use an incognito chat

Goal: keep a throwaway exchange from lingering in your library.

1. In the sidebar, next to **New Chat**, flip on the **incognito toggle** (it shows a lock).
2. Start your chat — the button reads **Incognito Chat** to confirm. The toggle resets afterward, so you opt in each time.
3. The chat auto-expires (one hour by default), skips long-term memory writes, and can't be shared.

Incognito is a local-retention control, not an anonymity feature — the conversation still runs through your configured provider like any other chat.

Full detail: [Organizing and sharing](09-organizing-and-sharing.md)

## Manage people (admin)

### Invite or promote an admin

Goal: give someone else admin rights, by invite or after the fact.

1. To invite: open **Admin: Users** (`/admin/users`) and click **Invite admin**. LM Chat mints a one-shot `/register?token=…` link — copy it and share it directly. Whoever registers through it lands as an admin. Tokens are single-use and expire after 24 hours.
2. To promote an existing member: on the same page, find their row and click **Promote**.

You can't demote or delete yourself.

Full detail: [Settings and admin](10-settings-and-admin.md)
