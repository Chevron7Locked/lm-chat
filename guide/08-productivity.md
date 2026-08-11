# Productivity

LM Chat is built for getting real work done, not just trading messages. This page covers the features that speed you up: talking to a chat instead of typing, letting a chat pull in fresh information from the web, saving the prompts you reuse, running two models against each other, and asking for a more careful answer when accuracy matters most.

Each of these is a separate, optional tool. Reach for the ones that fit the task in front of you and ignore the rest.

## Voice input

You can speak to a chat instead of typing. Every composer has a microphone button at the edge of the input box. Click it and start talking; LM Chat listens, turns your speech into text, and drops that text into the composer.

The transcript is **appended**, not substituted — anything you'd already typed stays put, and the spoken words land after it. So you can type the start of a thought, switch to voice for the long part, and edit the whole thing before you send. Nothing is sent automatically; you review and press send yourself.

To toggle listening from the keyboard, press **Cmd/Ctrl+Shift+M**. The same shortcut starts and stops a recording, so you can keep your hands on the keyboard. While LM Chat is listening, the mic button shows a recording indicator; click it again (or use the shortcut) to stop.

### Everything stays in your browser

Voice input runs entirely **in your browser**, using its built-in speech recognition. No audio is recorded to disk, and none of it is sent to LM Chat's backend or any model — only the final text transcript ever leaves the microphone, and it goes straight into your composer. Speaking to a chat is exactly as private as typing into it.

### When your browser doesn't support it

Browser speech recognition isn't universal. Chrome, Edge, and Safari support it; Firefox does not. On a browser without support, the mic button appears **disabled**, with a tooltip explaining that speech-to-text isn't available there. The rest of the composer is unaffected — you just type as usual. If the browser supports voice but you've denied microphone permission, LM Chat tells you that access was denied rather than failing silently.

## Web search

By default a model answers from what it already knows. Web search lets a chat reach out to the live internet first, so its answer can draw on current information instead of only its training data.

Web search is a **per-chat** setting. When it's on, LM Chat runs a search behind the scenes before the model answers, and folds the top results — page titles, URLs, and short snippets — into the context the model sees. The model then writes its reply with those results in hand, so it can cite recent pages and ground its answer in what's actually online right now.

Searches go through a privacy-respecting provider. The default is **SearXNG**, a self-hosted metasearch engine that doesn't profile you. If SearXNG isn't reachable or returns too little, LM Chat automatically falls back to **DuckDuckGo** so a search still returns something useful. Which provider is used, and the address of your SearXNG instance, are set once during setup — see [Settings and admin](10-settings-and-admin.md) for the administrative side.

Web search shares a goal with the **SearXNG** and **Fetch** tools in the MCP Store but works differently: web search is a built-in, lightweight "look it up before answering" step, while MCP tools hand the model an ability it can decide to call mid-conversation. For the tool route, see [MCP and tools](05-mcp-and-tools.md).

## Prompt library

If you keep rewriting the same instructions — a summarizing prompt, a code-review checklist, a particular tone you like — save it once and reuse it. That's what the prompt library is for.

Open it from **Prompts** in the sidebar. The page has two parts:

- **New prompt** — give your prompt a short **Name** (for example, `summarize-code`) and paste its **Content**, then click **Create prompt**. Names must be unique; if you reuse one, LM Chat tells you the name is taken.
- **Your prompts** — every prompt you've saved, listed by name. Each has an **Edit** action to change its name or content inline, and a **Delete** action to remove it.

### Using a saved prompt in a chat

Once a prompt is saved, pull it into any chat with the slash command **`/prompt <name>`**. Type `/prompt summarize-code` in the composer and LM Chat replaces the command with that prompt's content, ready for you to add to or send. The name match is case-insensitive, and if no prompt matches, LM Chat says so instead of sending an empty command. For more on slash commands generally, see [Chatting](02-chatting.md).

## A/B model compare

When you can't decide which model to trust for a task, run both. A/B compare sends the **same prompt to two models at once** and shows their answers side by side, so you can judge them directly instead of switching models and re-asking.

Turn it on in the chat's settings: enable A/B compare and choose the two models — **Model A** and **Model B**. You can also type `/compare` to jump straight to the model-picker. With it active, your next message fans out to both. The two answers **stream in parallel**, each in its own pane, so you watch them take shape at the same time rather than waiting for one then the other. Each pane carries its model's label and a live status — waiting, streaming, done, or error — and an error in one pane never stops the other.

When both are finished, each pane offers a **Use this response** button. Click the one you prefer and that answer is committed into the chat's history as the turn's reply; LM Chat then switches A/B compare back off, returning the chat to its normal single-stream mode. You can re-enable A/B compare from settings whenever you want to run another head-to-head. To learn how to pick and connect the models you compare, see [Providers and models](04-providers-and-models.md).

## Quality modes

Sometimes you want more than a quick answer — you want a *reliable* one. Quality modes ask the model to work harder on a single turn, trading extra time and tokens for a more trustworthy result. You'll find them in the chat-settings rail, under the **Quality** section, as two independent toggles. Leave them off for everyday chat; switch one on when an answer really has to be right.

### Self-consistency

Turn on **Self-consistency** and, instead of answering once, the model drafts the same prompt several times independently. LM Chat then compares those drafts against one another and returns the one that sits most squarely in the middle of the pack — the answer the model converged on across attempts, rather than a single lucky or unlucky roll.

The idea is simple: a correct answer tends to show up consistently across independent tries, while one-off mistakes don't agree with each other. By sampling a few answers and reconciling them, self-consistency filters out the flukes. It costs more — several generations instead of one — so it's best saved for questions where a wrong answer would be expensive.

### Chain-of-verification

Turn on **Chain-of-verification** and the model checks its own work before you see it. It runs in four steps:

1. **Draft** an initial answer to your prompt.
2. **Generate verification questions** that probe the load-bearing claims in that draft.
3. **Answer each question** on its own, in parallel, without the pressure of the original framing.
4. **Revise** the draft in light of those answers — correcting anything the checks turned up, or returning the original unchanged if it held up.

This catches the kind of confident-but-wrong detail a single pass tends to slip in, because the model has to defend its claims one by one before committing. Like self-consistency, it spends extra time and tokens for higher reliability, so it's most worth it on factual, multi-claim answers where a quiet error would matter.

You can run either mode on its own. Both ask more of the model than a plain reply, so expect a turn to take longer when one is active — that wait is the price of the extra rigor.

## Freeing up context

A long-running chat eventually fills up the model's context window. Rather than making you delete history to make room, `/compact` summarizes the oldest messages with a local model and archives them into a collapsed, expandable **folded recall tab** — nothing is deleted. The summary is injected into context so the gist carries forward, while the original messages stay one click away inside the tab whenever you want to see them in full. See [Chatting](02-chatting.md) for how to run it.

## Where to go next

- [Chatting](02-chatting.md) — slash commands, including `/prompt`, and the everyday composer.
- [Providers and models](04-providers-and-models.md) — connect the models you'll compare and talk to.
- [MCP and tools](05-mcp-and-tools.md) — give a model live abilities, including tool-driven web search.
- [Knowledge and memory](07-knowledge-and-memory.md) — ground answers in your own documents, not just the web.
- [Settings and admin](10-settings-and-admin.md) — configure the web search provider and other behind-the-scenes controls.
