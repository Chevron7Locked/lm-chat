# MCP and tools

LM Chat can give a model real abilities beyond text: search the web, read your files, query GitHub, run a headless browser, do exact math, and more. These abilities arrive as *tools*, and tools come from *MCP servers* — small programs that speak the Model Context Protocol.

This page explains where tools come from, how to install them, how to control which ones run, and how to switch them on for a chat.

## How tools reach the model

There are two separate paths, and which one is used depends on the model you're talking to. You don't have to choose between them by hand — LM Chat picks the right path automatically. It helps to know both exist, because they behave a little differently.

### LM Studio's own MCP servers

If you run a local model through LM Studio, any MCP servers you've configured *in LM Studio itself* are available right away. With the native LM Studio path, those tools run **server-side, inside LM Studio**. LM Chat simply passes the list of active tools along with your message, and LM Studio handles the calls and returns the results in the same response.

You manage these servers in LM Studio's own settings. They show up in LM Chat without any extra setup on the LM Chat side.

### LM Chat's MCP Store and native host

LM Chat also has its own MCP host. It runs MCP servers **itself, directly on your machine**, then feeds each tool result back to the model through an agentic loop — call a tool, read the result, decide what to do next, repeat until the model is done.

This is what lets **cloud models** use tools. Models from OpenAI, OpenRouter, and Groq can't reach LM Studio's server-side tools, so when you talk to a cloud model, LM Chat's native host steps in: it starts the MCP servers locally, executes the tool calls, and stitches the results into the conversation. LM Studio's own loop is left completely untouched — nothing about the local-model experience changes.

In short:

- Local model in LM Studio → tools run inside LM Studio.
- Cloud model → LM Chat's host runs the tools locally on your machine and loops the results back to the model.

The servers you install through the MCP Store (below) are the ones LM Chat's host runs.

## The MCP Store

The MCP Store is a curated catalog of well-known MCP servers you can install with one click. Open it from Settings. It ships 23 servers today (GitHub, databases, search, docs, browser automation, and more) — browse the full list in the Store. A representative sample:

- **GitHub** — read and write GitHub repos, issues, and pull requests.
- **Filesystem** — let the model read and write files in paths you expose.
- **Fetch** — fetch and parse web pages or raw URLs.
- **Sequential Thinking** — a structured, multi-step reasoning tool for breaking down hard problems.
- **Context7** — pull up-to-date library docs and code examples.
- **DeepWiki** — query deep documentation and wiki-style knowledge bases.
- **Crawl4AI** — self-hosted crawler/scraper: URL-to-Markdown, crawl, and screenshot capture. No vendor API key needed.
- **Firecrawl** — crawl and scrape websites into clean Markdown. Needs its own Firecrawl API key.
- **SearXNG** — privacy-respecting web search via a self-hosted SearXNG instance.
- **Playwright** — headless browser automation and screenshot capture.
- **Wolfram Alpha** — computational answers, math, and factual lookups.

To install one, click its **Install** button. That's all most servers need.

### Servers that need a secret

Some servers talk to an outside service and need an API key or token — GitHub needs a personal access token, Context7 and Wolfram Alpha need their own keys, SearXNG needs the URL of your instance, and so on. When a server requires a secret, the install panel expands and asks for it before the install goes through. Required fields are marked, and the install won't complete until they're filled in.

Your secrets are kept safe. They're stored **server-side, encrypted at rest**, and are never sent back to the browser. When you look at an installed server later, LM Chat shows you only *which* secrets are set — never their values.

## Custom servers

You're not limited to the catalog. You can add your own MCP server — one you wrote, or one that isn't curated yet — as a custom install.

A custom server needs a name, a slug (a short identifier), and a transport:

- **stdio** — a local command LM Chat launches, such as an `npx` or `uvx` invocation, with its arguments.
- **http** or **sse** — a server reachable at an `http(s)` URL.

Fill in the command (for stdio) or the URL (for http/sse), add any secrets the server needs, and install. From then on it behaves exactly like a catalog server.

## Allow and deny tools

Installing a server is your consent to run it — once it's installed, LM Chat may start it and call its tools. You stay in control of the details:

- **Enable or disable** a whole server with its toggle. Disabling a server disconnects it immediately, so it stops receiving any tool requests.
- **Allow or deny individual tools** within a server. Each installed server has an expandable panel listing every tool it offers. Deny the ones you don't want the model to call, and they're blocked even while the server is enabled. Everything else stays allowed.

This lets you keep a server you mostly trust while ruling out a specific capability you'd rather not expose.

## Using tools in a chat

Installing a server makes its tools *available*. Turning them on for a given message is a per-chat choice.

Above the composer is a tools bar — a row of pills, one per available tool source. Click a pill to switch that tool source on or off for the conversation. Only the tools you've switched on are offered to the model when you send your next message, and your selection sticks with that chat, so the same tools stay active as you keep talking.

A few things to expect:

- The tools bar only appears for models that are trained to use tools. For models that aren't, it's hidden, since offering tools to them would do nothing useful.
- When a lot of tools are available, the bar collapses into a short summary you can expand, so it never crowds the composer.

## Where to go next

- [Providers and models](04-providers-and-models.md) — connect LM Studio and add the cloud providers whose models use these tools.
- [Settings and admin](10-settings-and-admin.md) — the admin controls behind the MCP Store and provider setup.
