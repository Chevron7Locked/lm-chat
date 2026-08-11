# Troubleshooting & FAQ

Something not working? Start here. This page lists the problems people actually hit with LM Chat, what causes each one, and the exact fix. It ends with a short FAQ about privacy, offline use, and which models work.

If you're brand new, the [Quickstart](00-quickstart.md) walks you through getting a model talking in a few minutes. For deeper background on any area, follow the cross-links below.

## Start here: one cause behind most "everything is broken"

Before you chase a specific symptom, check one thing first, because it explains a surprising number of unrelated-looking failures.

**If you use LM Studio with its API server set to require a key, and the key LM Chat has saved is wrong or missing, almost everything looks broken at once.** The model picker goes empty, every chat says the model isn't loaded, and knowledge retrieval reports no embedding model — all at the same time. The single underlying cause is that LM Chat can't authenticate to LM Studio, so it can't even list which models are loaded.

The fix is always the same:

1. Open **Settings → LM Studio**.
2. Paste the API key that matches what LM Studio expects (or clear LM Studio's required-key setting if you don't want one).
3. Press **Test connection**. A green result that lists your loaded models means you're good.
4. **Save.**

Keep this in mind as you read the specific problems below. If several of them are happening together, this is almost certainly why.

## Model and connection problems

### "Model not loaded" or the model picker is empty

**What you see:** The model dropdown has nothing in it, or sending a message returns "LM Studio has no model loaded" / "Selected model isn't loaded."

**Why it happens:** One of three things:

- LM Studio isn't running, or isn't reachable at the base URL LM Chat is configured with.
- LM Studio is running but no model is actually loaded into memory.
- LM Studio requires an API key and LM Chat's saved key is wrong or missing (see the section above).

**Fix:**

1. Make sure **LM Studio is running** and you've **loaded at least one model** in it.
2. Open **Settings → LM Studio**. Check the base URL — it's `http://localhost:1234` when LM Studio runs on the same machine as LM Chat. (Running LM Chat in Docker? See "Docker can't reach LM Studio" below.)
3. Paste your LM Studio API key if it asks for one.
4. Press **Test connection**. You should see your loaded models listed.
5. **Save.** The picker repopulates.

If models are loaded in LM Studio but still don't appear, the cached list just needs a refresh: re-run **Test connection** in Settings, or hard-reload the chat page. More on providers and models lives in [Providers and models](04-providers-and-models.md).

### "Key cleared by secret rotation" banner

**What you see:** A banner reading *"LM Studio API key was cleared by a secret rotation. Models won't load until it's re-saved."*

**Why it happens:** LM Chat encrypts your saved API keys with a server secret. If that secret changes between restarts — common during development or after a config change — the stored key can no longer be decrypted, so LM Chat deliberately discards it at startup. It does this loudly, with this banner, instead of silently failing every request with a confusing authentication error.

**Fix:** Open **Settings → LM Studio**, re-paste your API key, and Save. The banner clears once the new key is stored. There's a related banner — *"LM Studio returned 401"* — which means the saved key is present but LM Studio rejected it; the fix is the same: re-enter the correct key.

### A cloud provider shows "unreachable"

**What you see:** In **Settings → Providers**, a provider row shows a red *unreachable* badge, often with a short reason. Testing the connection fails.

**Why it happens:** Usually a wrong base URL or a wrong/expired API key. For cloud providers it can also be a `401` (the provider rejected your key) or a timeout (the provider was slow or down).

**Fix:** Open the provider in **Settings → Providers**, double-check the **base URL** and re-enter the **API key**, then press **Test connection**. A successful test shows how many models are reachable. If the test reports `401`, the key is wrong. If it times out, the provider or your network is the issue — try again in a moment. See [Providers and models](04-providers-and-models.md) for the supported providers and their base URLs.

### Docker can't reach LM Studio

**What you see:** LM Chat runs in a Docker container and can't connect to LM Studio even though LM Studio is running fine on your machine.

**Why it happens:** Inside a container, `localhost` points at the container itself, not your host machine — so `http://localhost:1234` never reaches LM Studio.

**Fix:** In **Settings → LM Studio**, set the base URL to **`http://host.docker.internal:1234`**. That hostname resolves to your host machine from inside the container. Test connection, then Save.

### The default model won't change, or reverts to the old one

**What you see:** You set a new default model in Settings, but your open chat keeps using the previous model.

**Why this is expected:** The default model applies to **new chats**. Existing chats keep the model they were started with — each chat remembers its own model so a setting change never silently rewrites conversations in progress.

**Fix:** Start a new chat to use the new default, or change the model for the current chat directly from the model selector in the chat header. More on this in [Chatting](02-chatting.md).

## Chatting and modes

### "Thinking…" seems stuck

**What you see:** A mode like Research or Code sits on "Thinking…" for a long time with no visible output.

**Why this is usually fine:** Reasoning-heavy local models can take anywhere from 30 to 120 seconds to plan before they emit their first output token, especially while figuring out tool calls. The "Thinking…" state stays up the whole time. This is normal, not a hang.

**What to do:** Wait. If you genuinely need to stop, use the **Stop generation** button in the composer to end the stream cleanly. To avoid long pauses, switch to a lighter model from the picker. See [Personas and modes](03-personas-and-modes.md) for how the modes work.

### You typed a slash command but nothing happened

**What you see:** You typed something like `/research ...` and pressed Enter, but no mode launched.

**Why it happens:** Two common reasons:

- **No model is selected.** Pick a model first — slash modes need a model to run.
- **The slash palette intercepted Enter.** When the slash menu is open, Enter navigates the menu rather than sending.

**Fix:** Type the command and your message together, then send the whole thing in one shot with **Cmd+Enter** (macOS) or **Ctrl+Enter** (Windows/Linux). For example: type `/research what's the latest Vite release` and press Cmd/Ctrl+Enter — that launches the Research mode and sends your question at once. More on slash commands in [Chatting](02-chatting.md).

### The voice (microphone) button is disabled

**What you see:** The mic button in the composer is greyed out and won't activate.

**Why it happens:** Either your browser doesn't support speech recognition — **Firefox** doesn't, while Chrome, Edge, and Safari do — or the browser denied microphone access. Speech-to-text runs entirely in your browser; no audio leaves your machine.

**Fix:** Use Chrome, Edge, or Safari for voice input. If you're already on one of those and it's still blocked, check that you granted microphone permission for the site (your browser's address-bar permissions). Once supported and permitted, the mic toggles with **Cmd/Ctrl+Shift+M**.

## Knowledge, retrieval, and memory

### Retrieval returns nothing, or "no embedding model loaded"

**What you see:** A chat that should pull from your documents pulls nothing, and you see a warning that no embedding model is loaded — retrieval is being skipped.

**Why it happens:** Knowledge retrieval needs a separate **embedding model** loaded in LM Studio, in addition to your chat model. Common causes:

- No embedding model is loaded at all.
- LM Studio's idle timeout (TTL) quietly **unloaded** the embedding model after a period of inactivity.
- A project **pinned** a specific embedding model that isn't currently loaded.

When the embedding model is missing, retrieval doesn't error out — it silently skips, which is why answers can look like they're ignoring your documents.

**Fix:** Load an embedding model in LM Studio, then confirm it in **Settings → LM Studio** under *Active embedding model*. If a project pinned a model that isn't loaded, either load that model or re-pin to one you have. If an idle timeout keeps unloading it, raise LM Studio's idle TTL for that model. Full detail on retrieval and memory is in [Knowledge and memory](07-knowledge-and-memory.md).

## Sharing and sessions

### A share link shows "Link not found"

**What you see:** Opening a public share link lands on *"Link not found — this share link has expired, been revoked, or never existed."*

**Why it happens:** The link is no longer valid for one of three reasons: it was **revoked**, it **expired**, or the underlying **chat was deleted**. (Private/incognito chats also can't be shared by design, so a link to one resolves the same way.)

**Fix:** There's nothing to repair on the viewer's side. If it's your chat and you still want to share it, generate a **new** share link from the chat. See [Organizing and sharing](09-organizing-and-sharing.md).

### You got signed out mid-session

**What you see:** You were working and suddenly landed back on the login screen.

**Why it happens:** Sessions are cookie-based and expire after a set window, or when the server restarts with a new secret.

**Fix:** Sign in again — your chats are preserved. Nothing is lost.

## FAQ

### Is my data private?

LM Chat is local-first. Your chats, projects, and documents live in a local database on the machine running LM Chat, and there's no telemetry — it never phones home. The only data that leaves your machine is what you deliberately route to a cloud provider: if you add OpenAI, OpenRouter, Groq, or any OpenAI-compatible endpoint and send a message to one of its models, that prompt goes to that provider, exactly like any API client. With only LM Studio configured, everything stays on your own hardware. Tools that reach the web (search, docs lookup) make their own outbound calls when you use them.

### Does it work offline?

Yes, if you run it fully local. With LM Studio serving your models on the same machine, LM Chat needs no internet — chatting, projects, and document retrieval all work offline. You only need a network connection for features that reach out: cloud providers and web-using tools.

### Can other people use my instance?

LM Chat is a single-admin app. One admin account owns the configuration. You can add members, but they have read-only access to connections — they can't change your providers, keys, or admin settings. It's not built to be a multi-tenant public service. See [Settings and admin](10-settings-and-admin.md) for what admins versus members can do.

### Which models work?

Any model you load in LM Studio, plus any model exposed by a configured OpenAI-compatible provider (OpenAI, OpenRouter, Groq, or a custom endpoint). For retrieval over your documents you also need an embedding model loaded. The model picker shows what's currently available; embedding models are filtered out of the chat picker since they can't hold a conversation. See [Providers and models](04-providers-and-models.md).

### Can I use Anthropic's Claude models?

Only through an OpenAI-compatible gateway. LM Chat speaks the OpenAI-compatible API, so you can reach Claude models via a provider or proxy that exposes them in that format (for example, a gateway that translates to Anthropic). There's no built-in native Anthropic provider — point a custom OpenAI-compatible provider at a gateway that fronts the models you want.

---

Still stuck? The in-app **Help** page mirrors much of this with live links into your settings. For how everything fits together, see the [Overview](01-overview.md) and [Architecture](14-architecture.md). For step-by-step recipes, see [How-tos](11-how-tos.md), and for unfamiliar terms, the [Glossary](12-glossary.md).
