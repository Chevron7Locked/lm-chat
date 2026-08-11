# Knowledge and memory

LM Chat can ground replies in your own material. Upload documents and it pulls the relevant passages into context automatically. Pin what matters and it carries those insights forward across conversations. Both run locally, on your machine, under your control.

This page covers the two systems:

- **Documents and retrieval** — files you upload, and how LM Chat finds the right pieces of them when you ask a question.
- **Memory** — durable insights and past-conversation recall that follow you between chats.

For the projects that group documents and chats together, see [Projects](06-projects.md). For loading the embedding model that powers all of this, see [Providers and models](04-providers-and-models.md).

## Documents

The **Documents** page is your desk. Upload a file and LM Chat reads it, splits it into passages, and indexes those passages so they can be retrieved later when they're relevant to your question.

### Uploading files

Open **Documents** from the sidebar, or from the right-hand panel inside a chat. Drop files onto the upload zone, or click it to pick files. You can upload several at once.

Supported file types:

- **Plain text** (`.txt`)
- **Markdown** (`.md`)
- **HTML** (`.html`) — tags are stripped, text is kept
- **PDF** (`.pdf`) — text is extracted. Image-only or scanned PDFs with no text layer have nothing to pull from.
- **EPUB** (`.epub`) — chapters are read in reading order and tags are stripped, same as HTML.
- **Word** (`.docx`) — paragraph text is extracted.

The default maximum file size is **50 MB** per file. Other formats are rejected on upload.

Each document row shows its filename, type, size, chunk count, and upload date. Click a row to expand it and preview the passages LM Chat created. Hover a row to reveal actions: move the document to a project, or delete it.

### Where documents attach

Every document belongs to you. Beyond that, a document is either:

- **Loose** — available across your chats, not tied to any project.
- **In a project** — scoped to one project, so it only feeds chats inside that project.

Use the move control on a document row to assign it to a project or pull it back out. This matters for retrieval: a chat inside a project only sees that project's documents, which keeps unrelated material out of the conversation. See [Projects](06-projects.md).

### How a document becomes searchable

When you upload a file, LM Chat runs it through a short pipeline:

1. **Extract** — pull plain text out of the file based on its type.
2. **Chunk** — split the text into overlapping passages of roughly 500 tokens each, with a 50-token overlap so sentences that straddle a boundary aren't lost.
3. **Embed** — turn each chunk into a vector using the embedding model loaded in LM Studio.
4. **Index** — store the chunks and their vectors locally, alongside a full-text keyword index.

Once this finishes, the document's chunk count appears on its row and the passages are available to retrieval. Chunks remember which embedding model produced their vectors, which is what lets retrieval stay correct even if you switch models later.

## Retrieval (RAG)

Retrieval — also called RAG, for retrieval-augmented generation — is how your documents reach the model. You don't paste passages in by hand. When you send a message in a chat that has documents available, LM Chat searches your indexed chunks for the ones most relevant to what you just asked and folds them into the model's context for that turn.

The search runs two ways at once and blends the results: a **keyword** match (exact terms) and a **vector** match (meaning-based, via embeddings). Combining both catches passages that share your wording and passages that share your intent.

### Retrieval modes

A small badge sits near the composer showing which retrieval mode is active for the current chat. There are three:

- **Inline** — the chat is in a project and the project's whole document corpus is small enough to fit comfortably in the model's context window. The documents are kept close at hand rather than searched piece by piece.
- **Hybrid** — the default for most chats. LM Chat searches your chunks and pulls in only the top matches for each message. This is what you get for loose documents, large project corpora, and chats outside any project.
- **Focused** — the chat is pinned to a single document, and that one document is the corpus. Useful when you want every reply grounded in one specific file.

Hover the badge to see the supporting numbers — the project's corpus size and the threshold LM Chat used to choose between inline and hybrid.

### Smart auto-enable and the per-chat toggle

Retrieval is smart about when to run. If you haven't set a preference for a chat, it turns **on automatically when there are documents to search** — for a project chat, that means the project has documents; for a loose chat, that you've uploaded at least one document. A chat with nothing to retrieve from skips the search entirely, so it costs nothing.

You can override this per chat. Toggling RAG off for a chat stops document retrieval and past-conversation recall for that chat, even if documents exist. Toggling it on forces retrieval even before you've uploaded anything. The explicit toggle always wins over the automatic default.

### You need an embedding model loaded

This is the one thing to get right. Retrieval's vector search depends on an **embedding model loaded in LM Studio** — for example, `text-embedding-nomic-embed-text-v1.5`. Keep this in mind:

- **If no embedding model is loaded, vector retrieval is unavailable.** Keyword search still surfaces exact-term matches, but the meaning-based half of retrieval goes quiet, so results are weaker.
- The retrieval badge **warns you** when this happens. It collapses to a small amber dot; hover it and it tells you either "No embedding model loaded" or that the model your project pinned isn't currently loaded.
- LM Studio's **idle timeout can unload an embedding model** that hasn't been used in a while. If retrieval was working and then stopped, an unloaded embedding model is the first thing to check.
- A project can **pin** a specific embedding model. If that exact model isn't loaded, retrieval for the project is skipped rather than run against the wrong model — querying under a different model than your chunks were indexed with would return wrong matches.

If retrieval seems to be returning nothing, load an embedding model in LM Studio and try again. See [Providers and models](04-providers-and-models.md) and [Troubleshooting & FAQ](13-troubleshooting-faq.md).

## Memory

Memory is what lets a conversation build on what came before. It has two parts: **pinned insights** you choose to keep, and **recall** of your earlier messages.

### Pinned insights

A pinned insight is a durable note — a preference, a fact, a working agreement — that you want carried into your conversations going forward. Insights are injected into context for the model on every turn, so they reach the model whether or not retrieval is otherwise on for that chat.

Two ways to pin:

- **The `/memory` command.** Type `/memory` followed by the text in any chat — for example, `/memory I prefer concise answers with code first`. The insight is pinned and tied to that conversation.
- **The Memory page.** Open **Memory** from the sidebar or the right-hand panel and add an insight in the input at the top.

On the **Memory** page you can view every pinned insight, search across them, edit one in place, and unpin the ones you no longer want. There's also a **Refine** action that asks the model to merge duplicates and tighten the wording of your pinned set; you can undo a refine if you don't like the result.

Insights respect projects: an insight pinned inside a project stays with that project's chats and won't bleed into unrelated conversations.

### Recall of past messages

Alongside pinned insights, LM Chat quietly indexes your messages as you chat, so a later question can surface a relevant earlier exchange. This recall rides the same per-chat RAG toggle as document retrieval — turn RAG off for a chat and recall stops for it. Messages in incognito chats are not indexed.

### Reindexing (admin)

Memory vectors are stamped with the embedding model that produced them. If you switch embedding models, the older vectors were built in a different space and won't compare cleanly against new queries. Admins can run a **Reindex** from the Memory page to re-embed everything under a chosen model and bring the index back into a single, consistent space.

### Local and under your control

Your documents, your pinned insights, and your message index all live in LM Chat's local store on your machine. Nothing is sent anywhere except to the model endpoints you've configured. You can edit or unpin any insight, delete any document, and turn retrieval off per chat at any time.

A note on expectations: memory is a tool for grounding, not a guaranteed permanent record. Recall surfaces what's relevant rather than replaying everything, vectors depend on the embedding model that built them, and a reindex rewrites the index. Treat pinned insights as the durable layer you control directly, and keep anything you truly can't lose in a document or your own notes.

## See also

- [Projects](06-projects.md) — grouping documents and chats, and pinning a per-project embedding model
- [Providers and models](04-providers-and-models.md) — loading models in LM Studio, including embedding models
- [MCP and tools](05-mcp-and-tools.md) — tools and external sources beyond your own documents
- [Troubleshooting & FAQ](13-troubleshooting-faq.md) — when retrieval returns nothing, or the embedding model keeps unloading
- [Glossary](12-glossary.md) — RAG, embedding, chunk, and other terms
