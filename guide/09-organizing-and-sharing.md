# Organizing and sharing

Once you have more than a handful of chats, you'll want to keep them tidy and, now and then, hand one to someone else. This page covers the tools LM Chat gives you for that: folders, tags, archiving, pinned messages, share links, and incognito chats.

If you're new here, start with [00-quickstart.md](00-quickstart.md) and [02-chatting.md](02-chatting.md) first. For the bigger organizing unit — grouping chats, files, and instructions together — see [06-projects.md](06-projects.md).

## Folders and organizing chats

The sidebar is your chat library. Chats are grouped into three bands, top to bottom:

- **Pinned** chats, at the very top.
- **Folders** you've created, each with its own chats.
- **Ungrouped** chats, in one last band at the bottom.

### Create a folder

Use the new-folder control at the top of the chat list, type a name, and confirm. A folder is just a label you own — it's private to your account, and an empty folder still shows in the list so you can fill it later. Adding a folder name that already exists does nothing.

### Move chats into folders and reorder them

Every chat row has a drag handle. Grab it and drag a chat into a folder's group to file it there, or drag it within a group to reorder. Drag order is remembered per band, so the arrangement you set sticks across sessions.

You don't need a mouse: the drag handle is keyboard-operable. Tab to the handle, press Space or Enter to pick the chat up, use the arrow keys to move it, and press Space, Enter, or Escape to drop or cancel. A screen-reader announcement confirms each grab and drop.

You can also right-click (or open the row's context menu on) a chat to **Move to folder**, **Pin**, or **Unpin** without dragging.

### Pin a chat to the top

Pinning lifts a chat out of its folder and into the **Pinned** band at the very top of the sidebar — handy for the one or two conversations you return to constantly. Pin and unpin from the chat row's context menu, or from the **Pin** action in the chat's top-bar menu while you're reading it. Pinned chats keep their own drag order.

Pinning a *chat* is different from pinning a *message* (below) and from pinning an *insight* to memory — three separate features that happen to share the word "pin."

### Tag a chat

Tags are free-form labels you attach to a chat to group or find it across folders. Open the chat row's **tags** control in the sidebar, type a tag, and confirm; a small count badge then shows on the row. A chat can hold up to 20 tags (each up to 256 characters). Tags are private to your account and independent of folders — a chat can sit in one folder yet carry several tags.

### Archive a chat

Archiving hides a chat from your everyday list without deleting it. Use the **archive** action on the chat row; the chat drops out of the sidebar's main bands into a collapsible **Archived** section at the bottom. Expand that section and use **unarchive** to bring a chat back to the active list at any time. Archiving is reversible and keeps the whole conversation — the tidy alternative to deleting a chat you're done with but don't want to lose.

### Rename and delete chats

LM Chat names new chats for you from their first exchange; while that's happening the row shows a "Generating title…" placeholder. To rename a chat yourself, open its top-bar menu and edit the title.

To delete a chat, use the delete action on its sidebar row. Deletion asks you to confirm first, so a stray click won't lose a conversation.

### A note on projects

Folders organize your loose, everyday chats. **Projects** are a separate, richer space with their own chat list, knowledge base, and instructions — and projects do not use these folders. See [06-projects.md](06-projects.md).

## Pinned messages

Folders organize whole chats. **Pinned messages** work *inside* a single chat, so you can mark the few turns that matter — a spec, a decision, a snippet of code — and jump back to them without scrolling.

Pinned messages surface in two places:

- **Pin strip** — a slim horizontal row near the top of the chat showing each pinned message as a clickable chip. Click a chip to scroll that message into view. The strip only appears when the chat has pins.
- **Pinned messages panel** — opened from the chat's top-bar menu, this panel lists each pinned message with a longer preview and its role (you or the model). Click an entry to scroll to it; use the unpin button on any entry to remove it. Press Escape or click outside to close.

Pinned messages are remembered in your browser, per chat. They're a personal reading aid local to the device you set them on — they are not stored on the server and are not part of a share link.

### Pinned messages vs `/memory`

These two are easy to confuse:

- A **pinned message** is a bookmark. It points at an existing turn in *this* chat so you can find it again. Nothing is copied; nothing is sent to the model.
- The **`/memory`** command pins an *insight* into your long-term memory, which the model can then recall in future chats. That's a content store, not a bookmark.

In short: pin a message to **find** it later; use `/memory` to have something **remembered** later. Memory is covered in [07-knowledge-and-memory.md](07-knowledge-and-memory.md).

## Sharing a conversation

You can publish any normal chat as a read-only web page and send the link to anyone — no LM Chat account needed on their end.

### Create a share link

Open the chat's top-bar menu and choose **Share**. LM Chat mints a link of the form `/share/<token>` and copies it to your clipboard. The token is the only thing that grants access, so treat the link like a password: anyone who has it can open the page.

Sharing is idempotent — clicking **Share** again on an already-shared chat returns the same link rather than creating a new one.

### What the recipient sees

The link opens a clean, read-only page: a header with the chat's title and the date it was shared, the conversation rendered turn by turn, and a small "Powered by LM Chat" footer. That's all. The recipient cannot reply, edit, or continue the conversation, and the page never asks them to sign in.

The public view is deliberately minimal. It shows the message text (and any visible reasoning) but leaves out the private plumbing — no account details, no per-chat settings, no model identifiers.

The page is a **snapshot of the live chat**: it reflects the messages as they stand when the link is opened. If you keep talking in the chat afterward, viewers who open the link later will see the newer messages too — so stop sharing if a conversation moves somewhere you'd rather not publish.

### Stop sharing (revoke)

Open the same **Share** menu and revoke the link. From that moment the URL stops working: anyone who opens it — even with the exact token — gets a "Link not found" page that reads "This share link has expired, been revoked, or never existed." The same page appears if the underlying chat is later deleted. Revoking is the off switch; there's no separate expiry to wait for.

## Incognito chats

An incognito chat is a conversation you intend to be temporary. It's a **local-retention control** — a way to keep a throwaway exchange from lingering in your library — not an anonymity feature. The conversation still runs through your configured model provider exactly like any other chat; "incognito" only governs how long LM Chat keeps it and a couple of safety rails around it.

### Start one

In the sidebar, next to **New Chat**, there's an incognito toggle (it shows a lock when on). Flip it on and your next new chat is created incognito; the button label changes to **Incognito Chat** to confirm. The toggle then resets itself, so you opt in deliberately each time rather than accidentally creating a string of incognito chats.

Incognito chats are easy to spot in the sidebar: they sit slightly dimmed, in italics, with a small lock badge whose tooltip reads "Incognito chat — purged on logout or TTL expiry."

### It auto-expires

Each incognito chat is given a lifetime when it's created and is automatically swept once that time passes. The default lifetime is **one hour**, and LM Chat checks for expired chats on a short interval in the background, so an incognito chat doesn't stick around after its window closes. (An administrator can change the default lifetime; see [10-settings-and-admin.md](10-settings-and-admin.md).)

Because the whole point is to leave less behind, incognito chats also skip the long-term memory writes that a normal chat would make.

### Sharing is disabled for incognito chats

You cannot create a share link for an incognito chat — the **Share** row in its menu is greyed out and labelled "Share — disabled for incognito." This is enforced on the server as well, not just hidden in the menu, so an incognito conversation can never become a public page. The two features are intentionally mutually exclusive: incognito means *less* exposure, and sharing means *more*.

### Honest framing

To be clear about what incognito does and doesn't do:

- It **does** limit local retention: the chat auto-expires and is excluded from memory and from sharing.
- It **does not** anonymize you to your model provider, hide the request from network logs, or encrypt anything beyond what LM Chat does for every chat. If you need provider-level privacy, that's a provider and deployment question — see [04-providers-and-models.md](04-providers-and-models.md) and [10-settings-and-admin.md](10-settings-and-admin.md).

## Related pages

- [02-chatting.md](02-chatting.md) — the basics of a conversation.
- [06-projects.md](06-projects.md) — grouping chats, files, and instructions into a project.
- [07-knowledge-and-memory.md](07-knowledge-and-memory.md) — `/memory` and long-term recall.
- [08-productivity.md](08-productivity.md) — slash commands and other shortcuts.
- [13-troubleshooting-faq.md](13-troubleshooting-faq.md) — when a share link or folder isn't behaving.
