# Projects

A project bundles three things that belong together: a set of custom instructions, a folder of documents, and a collection of chats. Anything you put in a project is shared by every chat inside it — so a project is the place where context lives across more than one conversation.

Reach for a project whenever you have ongoing work that spans many chats: a codebase you keep returning to, a research topic with its own reference material, a writing project with its own voice. Set the instructions and attach the documents once, and every chat you start in the project picks them up automatically.

## What a project is

A project is the unit of cross-conversation context. It holds:

- **Instructions** — a custom system prompt that shapes how every chat in the project behaves.
- **A knowledge base** — documents you attach to the project, which its chats can search and draw from.
- **Chats** — the conversations that live inside the project and inherit both of the above.

A plain chat starts from nothing each time. A chat in a project starts already knowing your standing instructions and already able to reach your attached documents. That's the whole point: you stop re-explaining yourself at the top of every conversation.

## Creating a project

Projects live in their own section of the sidebar, sitting above your folders and chats.

To create one, click the **+** (New project) button in the Projects header. A small input appears inline — type a name and press **Create**. The new project opens immediately, ready for you to add instructions and documents.

Each project then shows up as a row in the Projects section. Click any row to open that project's page.

## The project page

Opening a project takes you to its own page, organized into three tabs:

- **Chats** — every chat in the project, plus a field to start a new one.
- **Documents** — the project's knowledge base: upload new files, attach existing ones, or remove them.
- **Settings** — the project's name, description, and custom instructions.

The tab you're on is remembered in the page address, so a refresh or a shared link lands you back on the same tab.

You can rename a project at any time by clicking its name at the top of the page.

## Project instructions

A project's custom instructions are a system prompt that applies to every chat inside it. Set them in the project's **Settings** tab, in the **Custom instructions** field. Use this to state the things that are true for all of your work in this project — the codebase you're in, the audience you're writing for, the conventions you follow, the tone you want.

To get started quickly, the Settings tab lets you pick one of your saved presets as a starting point. Selecting a preset **copies** its text into the instructions field; from there you can edit it freely. The copy is a snapshot — later changes to the preset won't reach back and alter this project.

### How the project prompt layers in

This is the important part to get right: the project prompt **prepends** the chat's persona prompt. It layers on top of the persona — it does not replace it.

When you send a message from a chat in a project, the project's instructions come first, then the chat's persona, then any per-chat custom instructions you've added in the chat-settings rail. So a project doesn't swap out the personality you've chosen for a chat — it sets the broad context that the persona then operates within. Pick the Coder persona inside a project about your codebase, and you get Coder's working habits *plus* your project's standing instructions, together.

For the full composition order and how the persona fits, see [Personas and modes](03-personas-and-modes.md).

## Project knowledge base

The **Documents** tab is your project's knowledge base. Documents you attach here are split into chunks, embedded, and made searchable, so chats in the project can pull relevant passages into their answers automatically — you don't have to paste anything in by hand.

There are two ways to add a document:

- **Upload** a new file, which goes straight into this project.
- **Attach existing** to pull in a document you've already uploaded but haven't assigned to a project.

You can **remove** a document from the project (it survives as an unassigned document) or **delete** it outright.

If you change your active embedding model after attaching documents, the project will warn you that its existing chunks were embedded under a different model and offer a **Re-embed all** button. Use it to re-encode every document under the new model so retrieval stays accurate.

For how retrieval works and what file types are supported, see [Knowledge and memory](07-knowledge-and-memory.md).

## Chats in a project

A chat created inside a project belongs to it. From the project's **Chats** tab, give a new chat a title and start it — it opens already carrying the project's instructions and able to retrieve from the project's documents.

The Chats tab lists every conversation in the project, so your work stays grouped in one place instead of scattered through your general chat history.

When you're inside a project chat, a small chip above the composer shows which project you're in. Click it to jump back to the project page. A chat without a project shows no chip — it's a plain conversation with no shared context.

## Project settings

The **Settings** tab is where you manage the project itself:

- **Name** — what the project is called, shown in the sidebar and on its page.
- **Description** — an optional one-line summary.
- **Custom instructions** — the project's system prompt, covered above.

Click **Save settings** to apply your changes.

The same tab holds **Delete project**. Deleting a project is deliberate — you confirm before it happens. When you do, the project is removed, but its chats and documents are **not** destroyed: they simply become unassigned and remain available on their own. Deleting a project clears the shared context; it doesn't throw away your conversations or files.
