# Personas and modes

Every chat in LM Chat has a personality. You choose how it behaves, what it sounds like, and what it's good at — and you can drop into a focused, single-purpose helper at any moment without disturbing that choice.

Two different controls do these two different jobs, and they're easy to mix up. This page keeps them straight:

- A **persona** is your chat's lasting personality. You set it once, and it shapes every reply until you change it.
- A **sub-agent mode** is a one-off. You trigger it with a slash command, it does a single focused task in a clean workspace, and it hands a summary back to your chat.

The short version: **personas are the setting; modes are the action.**

## What a persona is

A persona is the system prompt your chat runs on — the standing instructions that tell the model how to think and respond. When you pick a persona, you're choosing a personality and a set of working habits that stay in effect for every message you send in that chat.

A persona is **persistent**. Once you set it, it doesn't reset when you send a message, switch away and come back, or reload the page. It stays until you deliberately change it.

Each persona also carries sensible sampling defaults (such as temperature), so a fact-checking persona answers more conservatively and a creative one answers more freely, without you touching any sliders.

## The six personas (and None)

LM Chat ships with six built-in personas plus a raw escape hatch.

| Persona | Best for | Character |
| --- | --- | --- |
| **General** | Everyday conversation across any subject | Direct, opinionated, friendly. The default. |
| **Coder** | Writing and changing real code | Reads first, plans, makes the minimal change, verifies. |
| **Creative** | Stories, scripts, poetry, brand voice | A co-writer with craft — specificity over slop. |
| **Research** | Current facts, citations, verifiable claims | Searches before answering, cites sources, flags what's unverified. |
| **Analyst** | Reasoning over evidence you provide | Names assumptions, weighs alternatives, separates fact from judgment. |
| **Architect** | System design and tradeoffs | "It depends" — surfaces constraints, compares options, picks the boring one when it fits. |

**General** is the default. A new chat with no explicit choice runs as General — a knowledgeable, plain-spoken personality with a real point of view, not a form-filler.

Every persona shares one trait by design: it tells you the truth over telling you what you want to hear. Each will disagree when it has reason to, state its confidence plainly, and say "I don't know" rather than invent a confident-sounding answer.

### None — the raw escape hatch

**None** sends no system prompt at all. The model runs raw, with none of LM Chat's personality shaping or sampling defaults applied. Reach for this when you want to feel the underlying model's unguided behavior, or when you're bringing your own instructions and want nothing layered on top. None sits at the bottom of the persona list, clearly separated so it never reads as the default.

## Setting your chat's persona

Your chat's persona lives in the **chat-settings rail** — open it from the chat and look for the **Preset** selector at the top.

The rail picker is the **only** thing that sets your chat's persona. Choose a persona there and it's saved to that chat immediately; it applies to the next message and every message after, and it survives a reload. Nothing else writes this setting — not slash commands, not anything in the composer.

### The per-reply chip

Because a persona is persistent, the composer shows **no pill** for it — there's nothing transient to announce. Instead, **each reply is labeled with the persona's name**. Above every answer you'll see a small chip — "General", "Research", "Coder", and so on — in place of the raw model name, so you always know which personality produced the turn. (Pick None and there's no chip, because there's no persona in play.)

### Custom instructions on top

Under the persona selector, the rail has a **Custom instructions** box. Anything you type there is added to the chosen persona's prompt for this chat only — a way to amend behavior ("always answer in British English", "we're working in Rust") without abandoning the persona. The rail also shows a preview of the exact prompt your chat will send, so you can see how the pieces combine.

## Sub-agent modes

Sometimes you don't want to change your chat's whole personality — you just want one focused task done well, right now. That's what **sub-agent modes** are for.

Type a slash command in the composer to launch one:

| Command | Launches |
| --- | --- |
| `/research` | A Deep Research sub-agent |
| `/code` | A Coding sub-agent |
| `/write` | A Creative Writing sub-agent |
| `/analyze` | A Strategic Analyst sub-agent |
| `/architect` | A Systems Architect sub-agent |

When you fire one, three things happen:

1. **A clean workspace opens.** The sub-agent runs in its own fresh session. It sees only the task you give it — not your main chat history — so it isn't distracted or biased by everything that came before. A pill labeled "Research mode", "Coder mode", and so on marks that you're inside it.
2. **It does the work.** Have your exchange with the sub-agent: ask the research question, request the code, work through the analysis.
3. **It hands a summary back.** When you're done, press **Summarize → main chat**. The sub-agent writes up its result, and **Add to main chat →** drops that summary into your main conversation so it becomes part of the thread.

You can also pass your request inline — `/research what changed in the latest release` launches the mode and sends the question in one go.

Sub-agent modes are **transient**. A mode is a single, self-contained errand: it appears, runs, returns a summary, and is gone. It does **not** change your chat's persona — when you're back in the main conversation, the persona you set in the rail is exactly as it was. You can launch a mode from any chat no matter which persona it's using, and you can run one after another.

## Persona vs sub-agent: the difference

This is the distinction worth holding onto:

| | Persona | Sub-agent mode |
| --- | --- | --- |
| **What it is** | Your chat's standing personality (its system prompt) | A one-off helper for a single task |
| **How you set it** | The chat-settings rail picker | A slash command (`/research`, `/code`, …) |
| **How long it lasts** | Persistent — every message until you change it | Transient — one exchange, then it's done |
| **Where you see it** | A name chip on every reply (no composer pill) | A "…mode" pill while it's running |
| **Effect on the chat** | Defines how the whole chat behaves | None — your persona is untouched |
| **What it leaves behind** | Shapes all future replies | A summary added to your main chat |

A handy way to remember it: changing the **persona** is like reassigning who you're talking to for the whole conversation. Running a **mode** is like stepping next door to ask a specialist one question, then bringing their answer back.

The names overlap on purpose — the Research persona and `/research` mode share the same underlying instructions — but they're used differently. Set the **Research persona** when most of your conversation will be fact-finding. Use `/research` mode when you're in the middle of something else and just need one thing looked up.

## Per-preset model routing

Each persona can run on its own model. In **Settings → Preset Models**, you can pin a specific provider and model to any persona — so, for example, Research can run on a stronger model than your chat's everyday default, while Coder uses one tuned for code.

When a sub-agent mode launches, it uses the model pinned to that persona if you've set one; otherwise it falls back to the model your chat is currently using. This lets you match each kind of work to the model that does it best, without manually switching models every time. See [Providers and models](04-providers-and-models.md) for setting up providers and choosing models.

## How it all composes

When you send a message, LM Chat assembles the system prompt the model sees by layering, in this order:

1. **Project prompt** — if the chat belongs to a project with custom instructions, those go first. (See [Projects](06-projects.md).)
2. **Persona prompt** — your chosen persona's system prompt comes next.
3. **Custom instructions** — anything you typed in the rail's Custom instructions box is appended last, as your per-chat amendment.

In other words, a project's prompt **prepends** the persona's prompt, and your per-chat amendment is added below both. Each layer narrows the one above it: the project sets the broad context, the persona sets the personality, and your custom instructions fine-tune this one chat. The chat-settings rail shows you the fully composed result, so there's never any guesswork about what reaches the model.

Sub-agent modes sit outside this stack entirely. They run in their own clean session with just the mode's instructions — which is exactly why they're useful for a focused task, and why they leave your chat's composition untouched.
