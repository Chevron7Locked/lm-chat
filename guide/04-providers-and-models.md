# Providers and models

LM Chat talks to two kinds of backends: your local **LM Studio** instance and any **OpenAI-compatible cloud provider** you choose to add. Local is the default and the primary path; cloud is there when you want a frontier model. Both feed a single, merged model picker, and each chat routes to whichever model you pick for it.

This page covers connecting LM Studio, adding cloud providers, how the model picker works, the difference between your global default model and a chat's own sticky model, and the admin tools for loading and unloading models.

## LM Studio: the native path

LM Studio is LM Chat's primary backend. LM Chat reaches it over plain HTTP using LM Studio's **native** API (`/api/v1/chat`), not the OpenAI compatibility layer that most third-party clients settle for.

That choice is deliberate. The native path is what unlocks the features that make LM Chat more than a thin chat box:

- **Real MCP tool execution** — your LM Studio MCP servers run and return results inside the conversation.
- **Server-managed response chaining** — long conversations stay fast because LM Chat chains turns by reference instead of resending the whole history every time.
- **Live reasoning streaming** — for reasoning models, the thinking stream is surfaced as it arrives.
- **Per-model capability detection** — LM Chat reads each model's flags from LM Studio and shows whether it supports vision, tool use, and reasoning, right in the picker.

LM Chat never reads LM Studio's config files, never shells out to a CLI, and never assumes the two run on the same host. Loopback, your LAN, a Tailscale address, or a tunnel all use the same code path.

### Connecting and changing the connection

Open **Settings → LM Studio**. You'll see the active connection: base URL, API key, and default model, each tagged with where its value came from (your own override, the server admin default, or the environment).

- **Base URL** — where LM Studio is listening. On the same machine as LM Chat, use `http://localhost:1234`. Inside Docker, use `http://host.docker.internal:1234`.
- **API key** — only needed if you've turned on authentication in LM Studio. The saved key is never shown back to you; the field shows a placeholder when one is stored. Leave it blank to keep the existing key, or type a new one to replace it.
- **Default model** — pick from the dropdown of detected models, or switch to manual entry to pre-seed a model id that isn't loaded yet.

Click **Test connection** to run a one-shot probe. On success it reports how many models are reachable; on failure it tells you what went wrong (for example, that LM Studio is asking for an API key you haven't set). Then **Save**.

If LM Studio reports no embedding model, the same page flags it — load one (such as `text-embedding-nomic-embed-text-v1.5`) to enable memory and document search.

## Adding a cloud provider

Beyond LM Studio, LM Chat can route chats to **OpenAI**, **OpenRouter**, **Groq**, or any other **OpenAI-compatible** endpoint. Everything is configured in the app — there are no environment variables to edit.

Cloud providers are **admin-only**. Open **Settings → Providers** to see what's configured and add more.

> Native Anthropic is not a built-in provider. To use an Anthropic model, reach it through an OpenAI-compatible gateway (such as OpenRouter) added as a provider.

### The add-provider form

Click **Add provider** and fill in:

- **Provider** — pick a preset (OpenRouter, Groq, OpenAI) to prefill the base URL, or choose **Custom** and supply your own slug for any other OpenAI-compatible endpoint.
- **Base URL** — the provider's API root (for example `https://openrouter.ai/api`).
- **API key** — stored securely and never displayed back. When editing, leave it blank to keep the current key.
- **Default model** *(optional)* — a model id to use when a chat on this provider doesn't specify one.
- **Enabled** — turn the provider on or off without deleting it.

Click **Test connection** to probe the endpoint with your credentials. On success it reports the number of models reachable and unlocks the model allowlist below; on failure it surfaces the error so you can fix the URL or key before saving.

### Curating models with an allowlist

After a successful test, a searchable checklist of the provider's models appears. This is the **allowlist** — the models from this provider that you want to appear in the picker.

- Leave it **empty to allow all** of the provider's models.
- Select specific models to **restrict the picker** to just those. Cloud catalogs can run to hundreds of entries, so an allowlist keeps the picker focused on the handful you actually use.

Filter, select or deselect in bulk, then **Save**. The provider list shows a small badge with the allowlist count when one is active, and a reachability indicator updated in the background.

Saving takes effect immediately — no restart. You can edit, disable, re-test, or delete a provider at any time; deleting asks for confirmation.

## The model picker

Local and cloud models merge into **one picker**, grouped by provider: an **LM Studio** group first, then a group for each enabled cloud provider. Within the LM Studio group, loaded models are separated from those that aren't yet loaded, and each model shows capability icons for vision, tool use, and reasoning where it supports them.

This same picker appears wherever you choose a model — in a chat's header, and in the default-model fields in Settings. The per-provider allowlist is what decides which cloud models show up here.

## Default model vs per-chat model

There are two levels of model selection, and it helps to keep them straight.

- **The global default** is the model new chats start with. By default it's the **first model loaded in LM Studio**, so LM Chat just works without pinning anything. You can set a specific default in **Settings → LM Studio**, and you can pin one via the `LM_STUDIO_DEFAULT_MODEL` environment variable (empty by default, meaning "first loaded"). Changing the default applies to **new** chats going forward.

- **Each chat keeps its own model.** The first time you pick a model for a conversation, that choice sticks to that chat. Switching models in one chat never affects another, and changing the global default later won't disturb a chat that already has its own model. A chat uses, in order: the model you've selected for it, then its saved model, then the current global default.

This is why a new conversation follows your default while an older one stays on whatever you set it to.

## Managing loaded models

Admins get a dedicated **Models** page (under Admin) for managing what LM Studio has loaded. Load and unload operations are proxied straight through to LM Studio.

The page lists every model LM Studio knows about, with its size, parameter count, quantization, and load status. For each one you can:

- **Load** a model. If it already has one or more instances loaded, LM Chat confirms before loading another.
- **Unload** a model. With a single loaded instance, one click unloads it. With several, a picker lets you unload one instance or, with a confirmation step, all of them at once.

**Refresh** forces LM Chat to re-probe LM Studio so the list reflects what's actually loaded right now. The same "Refresh list" action is available next to the default-model picker in Settings.

Model **downloading** through LM Chat depends on your LM Studio version and may not be exposed in the page; the most reliable way to add new models is to download them in LM Studio directly, then refresh the list here.

## Where to go next

- [MCP and tools](05-mcp-and-tools.md) — how MCP servers work, and how cloud models get tool use through LM Chat's own MCP host.
- [Personas and modes](03-personas-and-modes.md) — set a chat's persistent character and run transient sub-agent modes.
- [Quickstart](00-quickstart.md) — connect LM Studio and send your first message.
