# Chatting

Chatting in LM Chat happens in one place: the composer at the bottom of the screen and the stream of messages above it. This page covers how to send a message, what streaming and reasoning look like, the actions on each message, the slash palette, and every keyboard shortcut.

## Sending a message

Type into the composer and press **Enter** to send. The composer grows to fit a few lines as you type and resets after each send.

- **Enter** sends your message.
- **Shift+Enter** inserts a newline without sending — use it for multi-paragraph or code-heavy prompts.
- **Cmd/Ctrl+Enter** also sends, for the muscle memory of people who expect that chord elsewhere.

You need a model selected before you can send. Until then the Send button stays disabled, and if you try to send a slash command first, LM Chat reminds you to pick a model and keeps your text so you don't have to retype it.

Above the input you may see a few quiet indicators: a chip for the project a chat belongs to, a retrieval-mode badge when document search is active, a tools row for any MCP integrations enabled in this chat, and a context meter showing roughly how much of the model's loaded context window you've used. The meter turns amber as you approach the limit.

### Attaching images

When the selected model supports vision, a paperclip button appears in the composer. Use it to attach images (JPEG, PNG, GIF, WebP, SVG) or a plain-text file, up to 10 MB each. Attachments show as small chips above the input and are sent with your next message; remove one with its `×` before sending. If the current model has no vision capability, the button is hidden — there's nothing to misclick.

### Voice input

If your browser supports speech recognition, a microphone button sits in the composer. Click it (or press **Cmd/Ctrl+Shift+M**) to dictate; the transcript is appended to whatever you've already typed, so you can mix voice and keyboard. Recognition runs entirely in your browser — no audio leaves your machine.

## Streaming and reasoning

Replies stream in token by token, with a soft caret blinking at the leading edge while the model writes. You can read along as the answer forms, and the **Stop** button replaces Send during a stream — click it to end the reply early. A stopped reply keeps whatever text arrived and is marked **Stopped**. If a reply runs into the model's output limit, it's marked **Cut off**; send another message to have the model continue.

Reasoning models stream their thinking separately. You'll see a block labeled **Thinking…** while the model reasons, which switches to **Reasoning** once the answer begins. This block is collapsed by default — expand it to read the chain of thought, or leave it closed. A spell of "Thinking…" with no visible answer yet is normal for these models, not a hang; the reply follows once the model finishes reasoning. To expand or collapse every reasoning block at once, press **Cmd/Ctrl+J**.

When a model uses a tool, the call appears as a collapsible card showing the tool's friendly name, its status, and — when you expand it — the arguments and result. Document citations render as inline badges you can click to see the source passage.

Each reply is labeled with the name of the persona that produced it — a small chip such as "Research" or "Coder" above the bubble — rather than the raw model id. That's how you tell at a glance which character or sub-agent mode wrote a given turn. See [Personas and modes](03-personas-and-modes.md) for what those labels mean.

## Message actions

Hover a message (or focus it) to reveal an action bar. Which actions appear depends on the message:

- **Edit** (your messages) — rewrite a message you already sent. Saving re-runs the conversation from that point, so the model answers your revised wording. Press **Cmd/Ctrl+Enter** in the edit box to save, or **Esc** to cancel.
- **Regenerate** (replies) — ask the model for a fresh take on the same prompt, replacing the current reply.
- **Copy** (your messages and replies) — copy the message text to your clipboard.
- **Delete** (your messages and replies) — remove a single message. LM Chat asks you to confirm first, and the removal can't be undone.

To pin a lasting insight rather than act on a single message, use the `/memory` command described below.

## The slash palette

Type `/` as the first character in the composer to open the slash palette. Keep typing to filter, use the arrow keys to move through the list, and press **Tab** or **Enter** to run the highlighted command. **Esc** closes the palette. You can also open the same list as a command palette with **Cmd/Ctrl+/**.

The palette has two groups. The first is **sub-agent modes** — `/research`, `/code`, `/write`, `/analyze`, and `/architect` — which run a single exchange in a clean, isolated context. Those are explained in [Personas and modes](03-personas-and-modes.md), so they aren't repeated here.

The second group is the **utility commands**:

- **`/clear`** — clear this chat's history. LM Chat asks you to confirm before anything is removed.
- **`/memory <text>`** — pin a durable insight to the conversation's memory, so it carries forward without you repeating it.
- **`/compact`** — summarize the oldest part of the conversation with a local model, then archive it into a collapsed **folded recall tab** rather than deleting it. Expand the tab any time to see the original messages in full. The summary is injected into context so the gist carries forward while freeing up room for new turns.
- **`/fork`** — branch the conversation from this point into a new chat, leaving the original untouched.
- **`/prompt <name>`** — insert one of your saved prompts into the composer, ready to edit before you send.
- **`/help`** — list every available command.

## Keyboard shortcuts

Press **?** anywhere outside a text field to open the full shortcuts reference. Here's the complete list:

| Shortcut | Action |
| --- | --- |
| Enter | Send message |
| Shift+Enter | Insert a newline |
| Cmd/Ctrl+Enter | Send message |
| Cmd/Ctrl+N | New chat |
| Cmd/Ctrl+Shift+S | Toggle sidebar |
| Cmd/Ctrl+, | Open settings |
| Cmd/Ctrl+K | Focus chat filter |
| Cmd/Ctrl+/ | Open command palette |
| Cmd/Ctrl+Shift+M | Toggle voice input |
| Cmd/Ctrl+Shift+L | Cycle theme (dark → light → system) |
| Cmd/Ctrl+J | Toggle thinking blocks |
| Cmd/Ctrl+Shift+E | Open chat export menu |
| / | Open the slash palette (as the first character) |
| ? | Show keyboard shortcuts |
| Esc | Close the open overlay (palette, panel, or menu) |

On macOS these use Cmd; on Windows and Linux they use Ctrl. The `?` and Esc keys work on their own, with no modifier.
