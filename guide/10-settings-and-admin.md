# Settings and admin

Settings is where you shape LM Chat to fit you: your profile, your password and second factor, how the app looks, and what defaults new chats start with. If you're the admin, the same area is also where you manage people, models, usage, and the connections everyone shares.

LM Chat is a local-first app with a single admin. The first person to register becomes the admin; everyone else is a regular member until the admin promotes them. That keeps the model simple — one person owns the connections and the roster, and everyone else just uses what's been set up for them.

## Opening Settings

Open your account menu (top right) and choose **Settings**, or go straight to `/settings`. Admin-only pages — Users, Models, Quotas, and Integrations — live under their own **Admin** links in the same menu and only appear when you're signed in as an admin. The **Analytics** link is available to everyone.

## The Settings page

Settings is a single page with a sidebar of tabs grouped into five sections. Pick a tab on the left (or tap the section label on a narrow screen to open a slide-in panel) and the matching panel opens on the right. Each tab has its own address, so `/settings/login-security` or `/settings/appearance` takes you straight there.

### Account

- **Profile** — Your presentable identity beyond the login name: email, display name, and an avatar URL. All three are optional; leave them blank or fill them in as you like. The avatar must be an `http(s)` link.
- **Login & Security** — Shows your username and role, lets you change your password, lets you sign out, and manages two-factor authentication (TOTP) — all in one place.

### Models

- **LM Studio**, **Providers**, **Preset models** — Where you connect model backends. See [Connections](#connections). Admin-only.

### Memory

- **Memory** — Toggles for auto-memory and sub-session memory distillation, the web-search provider, and a read-only view of your pinned-insights limit and indexing status. Memory itself is covered in [07-knowledge-and-memory.md](07-knowledge-and-memory.md).

### Tools

- **MCP Servers** — The MCP Store: browse and install MCP servers, manage installed servers, and add custom ones. Admin-only.
- **Integrations** — Browse enabled integrations. See [Connections](#connections).

### Preferences

- **Appearance** — Theme, text size, chat density, and message style. See [Appearance](#appearance) below.
- **Chat** — Defaults for new chats, starting with your default model. A per-chat choice from the model picker in the chat top bar always wins over this default.
- **Quota** — Your daily request and token usage shown as progress bars, with a countdown to when they reset.
- **Developer** — Power-user toggles, including verbose console logging for debugging.

Most tabs are available to everyone. The model-backend and MCP tabs — **LM Studio**, **Providers**, **Preset models**, and **MCP Servers** — are admin-only and don't appear for a regular member at all; in a single-admin app, one person owns the connections and the roster. **Integrations** is the exception: everyone sees the read-only catalogue of enabled tools, while an admin gets the controls to manage them. The dedicated Admin pages — Users, Models, Quotas, Integrations — are separate from Settings entirely and only an admin can reach them.

## Login & Security

The **Login & Security** tab is the home for your sign-in identity and second factor — everything that used to live across a separate "Account" tab and a separate "Security" tab is now in one place.

- **Username and role** — Shown read-only. The role is either *admin* or *user*.
- **Change password** — Enter your current password, then your new one twice. Passwords have to meet a minimum strength policy, checked on the server. Changing your password signs out all your *other* sessions but keeps you signed in where you are.
- **Sign out** — Two-step: the first click asks you to confirm, the second commits.

Further down the same page, **Two-factor authentication** lets you add a second factor with any standard TOTP authenticator app (1Password, Authy, Google Authenticator, Bitwarden, and the like):

1. Start setup. LM Chat shows an `otpauth://` enrollment link and a secret — no QR code is rendered.
2. Paste the link, or type the secret by hand, into your authenticator app.
3. Enter the six-digit code it generates to confirm. Once confirmed, you'll be asked for a code at each sign-in.

To turn two-factor off again, you re-enter your account password — a stolen session alone can't downgrade your second factor.

## Appearance

The **Appearance** tab has four controls:

- **Theme** — Dark (the default), Light, or System (follows your operating system). The switch animates from the button you click and respects a reduced-motion preference if you've set one. A live preview card updates instantly so you can see the palette before committing.
- **Text size** — Compact, Default, or Large. Scales the type system globally.
- **Chat density** — Comfortable (more breathing room between turns) or Compact.
- **Message style** — Bubbles (user messages get a tinted surface) or Flat.

Chat defaults like your default model live on the **Chat** tab instead.

## Connections

Three Settings tabs handle the model backends, and two handle tools. These are described in full elsewhere — here's just the map:

- **LM Studio**, **Providers**, and **Preset models** are where models come from. An admin connects LM Studio and any OpenAI-compatible providers, picks which models are allowed, and assigns a default model to each preset. Regular members use the resulting curated list. Full detail is in [04-providers-and-models.md](04-providers-and-models.md).
- **Integrations** and **MCP Servers** are where tools come from. The **MCP Store** lets an admin browse and install MCP servers in a click; members use the curated integrations the admin has enabled. Full detail is in [05-mcp-and-tools.md](05-mcp-and-tools.md).

If you're a regular member and a connection field looks read-only, that's expected — connections are an admin responsibility in a single-admin app.

## Admin: users and roles

The first account created on a fresh install is the admin, granted automatically at registration. Everyone who registers after that is a regular member with no access to the admin pages — they consume the curated model, provider, and integration lists, and that's it. Members can't see Users, Models, Quotas, or Integrations admin pages, and they can't change shared connections.

The **Admin: Users** page (`/admin/users`) lists every account with its role, when it was created, and when it last signed in. For each person an admin can:

- **Promote** a member to admin, or **Demote** an admin back to member. You can't demote yourself.
- **Revoke sessions** — sign that person out everywhere, forcing them to sign in again.
- **Delete** the account. Deleting cascades through that user's chats, messages, and sessions; the audit-log entries stay. You can't delete yourself.

### Inviting another admin

Rather than promote after the fact, an admin can hand out an **admin invite**. Click **Invite admin** to mint a one-shot invite link of the form `/register?token=…`, then copy it and share it directly. Whoever registers through that link lands as an admin straight away. Invite tokens are single-use and expire after 24 hours.

Regular members can register on their own without an invite (subject to how the install is set up); the invite is specifically the shortcut to creating *another admin*.

## Admin: model lifecycle

The **Admin: Models** page (`/admin/models`) lists every model LM Studio knows about, with its size, parameter count, quantization, and load status. From here an admin can **Load** and **Unload** model instances — those actions are proxied straight to LM Studio:

- With no instances loaded, **Unload** is disabled.
- With one loaded, **Unload** acts immediately.
- With several loaded, **Unload** opens a picker so you can drop one instance or, with a confirmation step, all of them.

**Refresh** re-probes LM Studio's catalog so newly downloaded or changed models show up. For how loading, defaults, and provider models fit together, see [04-providers-and-models.md](04-providers-and-models.md).

## Admin: analytics

The **Analytics** page (`/analytics`) opens for everyone and starts with your own numbers:

- A headline trio: an estimate of what you saved by running locally versus the cloud, your tokens used today, and your last stream's speed in tokens per second.
- Totals for messages and chats, plus messages in the last seven days.
- An activity chart over the last 14 days.
- Your top models by usage.

If you're an admin, the page adds a **System** section below your own stats with all-user aggregates: total users, total chats, total messages, recent activity, and the system-wide top models. Regular members see only their own figures.

## Admin: quotas

Every member has a daily allowance of requests and tokens. The defaults are generous — **100,000 tokens** and **1,000 requests** per day — and most installs never need to touch them.

Anyone can see their own allowance and today's usage on the **Quota** tab in Settings. To change someone's limits, an admin uses the **Admin: Quotas** page (`/admin/quotas`): it lists users who have a custom limit set, and an admin can edit any user's tokens-per-day and requests-per-day inline. Users who've never been given a custom limit simply use the system defaults and don't appear in the list until you set one.

Admins aren't blocked when they hit a limit — their usage is still recorded for the Analytics figures, but requests aren't refused.

## Related pages

- [01-overview.md](01-overview.md) — how the pieces of LM Chat fit together
- [04-providers-and-models.md](04-providers-and-models.md) — connecting LM Studio and providers, and managing models
- [05-mcp-and-tools.md](05-mcp-and-tools.md) — tools, integrations, and the MCP Store
- [07-knowledge-and-memory.md](07-knowledge-and-memory.md) — long-term memory and knowledge
- [13-troubleshooting-faq.md](13-troubleshooting-faq.md) — fixes for sign-in, model, and connection problems
