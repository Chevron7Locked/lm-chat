# API reference

This page enumerates the HTTP API that backs the LM Chat single-page app. The same FastAPI backend that serves the SPA exposes these routes, so any script, integration, or external client can drive LM Chat by speaking to them directly.

For how these pieces fit together at the system level, see 14-architecture.md. For the admin surface those endpoints power, see 10-settings-and-admin.md.

## Base path and conventions

- **Base path** — all application endpoints live under `/api`. A handful of operational and shell routes (health probes, SPA shell, manifest) live at the root.
- **Transport** — JSON request and response bodies unless noted. Some write endpoints take `application/x-www-form-urlencoded` or `multipart/form-data` (uploads, several settings writes); the streaming endpoints return Server-Sent Events (SSE).
- **Single-admin model** — LM Chat is a local-first, single-admin application. The first account created becomes the admin; admin-gated endpoints are operator tooling, not multi-tenant management.
- **Versioning** — there is no version prefix in the path. The OpenAPI document on disk (`docs/api/openapi.yaml`) is the machine-readable schema for request and response bodies.

## Authentication and gating

Authentication is **session-cookie based**. A successful `POST /api/auth/login` issues an `HttpOnly` cookie named `lmchat_session` with `SameSite` set so cross-origin POSTs are blocked. Browsers attach it automatically; scripted clients must persist and replay the cookie.

- **Public** — reachable without a session: setup/login/register flows, the public share view, and the operational probes.
- **User** — requires a valid session; returns `401` otherwise.
- **Admin** — requires a session belonging to the admin account; returns `401` if unauthenticated, `403` if authenticated but not admin. Marked **Admin** in the tables below.

**CSRF** — the session cookie is `HttpOnly` with a `SameSite` policy, which blocks the cross-origin form-POST vector. There is no separate CSRF token header; the cookie attributes are the defense. Keep this in mind if you front the API with a different origin.

## Auth and account

Registration, login, profile, password, and TOTP (two-factor) management. The setup/login/register routes are reachable without a session so the first admin can be created.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/auth/setup_status` | One-bit `needs_setup` signal for the login page | Public |
| POST | `/api/auth/register` | Create an account (setup token or admin invite) | Public |
| POST | `/api/auth/login` | Authenticate and issue the session cookie | Public |
| POST | `/api/auth/logout` | Revoke the session and clear the cookie | Public |
| GET | `/api/auth/me` | Current account profile | User |
| GET | `/api/auth/me/probe` | Lightweight signed-in check | Public |
| PATCH | `/api/auth/profile` | Update profile fields | User |
| POST | `/api/auth/password` | Change password | User |
| POST | `/api/auth/totp/setup` | Begin TOTP enrollment | User |
| POST | `/api/auth/totp/verify` | Confirm and enable TOTP | User |
| POST | `/api/auth/totp/disable` | Disable TOTP | User |

## Chats

Create, read, organize, fork, compact, and share chats. Per-chat RAG mode, title generation, and message injection live here too.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| POST | `/api/chats` | Create a chat | User |
| GET | `/api/chats` | List chats | User |
| PATCH | `/api/chats/reorder` | Reorder chats | User |
| GET | `/api/chats/{chat_id}` | Fetch a chat with its messages | User |
| PATCH | `/api/chats/{chat_id}` | Update chat metadata (title, folder, settings) | User |
| DELETE | `/api/chats/{chat_id}` | Delete a chat | User |
| GET | `/api/chats/{chat_id}/rag_mode` | Read the chat's RAG mode | User |
| DELETE | `/api/chats/{chat_id}/messages` | Clear all messages in a chat | User |
| POST | `/api/chats/{chat_id}/messages` | Append a message | User |
| POST | `/api/chats/{chat_id}/messages/{message_id}/regenerate` | Regenerate a reply | User |
| POST | `/api/chats/{chat_id}/fork` | Fork the chat | User |
| POST | `/api/chats/{chat_id}/compact` | Summarize and archive the oldest message span (hybrid compaction) | User |
| GET | `/api/chats/{chat_id}/compactions` | List a chat's compaction spans (folded recall tabs) | User |
| GET | `/api/chats/{chat_id}/compactions/{compaction_id}/messages` | Read the archived messages for one compaction span | User |
| POST | `/api/chats/{chat_id}/generate-title` | Auto-generate a title | User |
| POST | `/api/chats/{chat_id}/inject-message` | Inject a message into the chat | User |
| POST | `/api/chats/{chat_id}/share` | Create a public share link | User |
| GET | `/api/chats/{chat_id}/share` | Read the chat's share state | User |
| DELETE | `/api/chats/{chat_id}/share` | Revoke the share link | User |
| POST | `/api/chats/{chat_id}/sub-session/stream` | Stream a sub-session (SSE) | User |
| POST | `/api/chats/{chat_id}/sub-session/finalize` | Finalize a sub-session | User |

## Messages

Per-message edits, deletes, and feedback (thumbs and notes).

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| PATCH | `/api/messages/{message_id}` | Edit a message | User |
| DELETE | `/api/messages/{message_id}` | Delete a message | User |
| POST | `/api/messages/{message_id}/feedback` | Submit feedback (rating + note) | User |
| GET | `/api/messages/{message_id}/feedback` | Read feedback | User |
| DELETE | `/api/messages/{message_id}/feedback` | Remove feedback | User |

## Streaming and comparison

The primary chat completion path is an SSE stream. The A/B endpoint streams two model responses side by side for comparison.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| POST | `/api/chat/stream` | Stream a chat completion (SSE) | User |
| POST | `/api/ab/stream` | Stream an A/B model comparison (SSE) | User |

## Projects

Projects group chats, documents, and custom instructions. Documents uploaded to a project are embedded for retrieval.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| POST | `/api/projects` | Create a project | User |
| GET | `/api/projects` | List projects | User |
| GET | `/api/projects/{project_id}` | Fetch a project | User |
| PATCH | `/api/projects/{project_id}` | Update a project | User |
| DELETE | `/api/projects/{project_id}` | Delete a project | User |
| POST | `/api/projects/{project_id}/chats` | Create a chat in the project | User |
| POST | `/api/projects/{project_id}/documents` | Upload a document to the project | User |
| POST | `/api/projects/{project_id}/re-embed` | Re-embed project documents | User |

## Documents

Standalone document upload, listing, deletion, metadata edits, and chunk previews for retrieval-augmented chat.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| POST | `/api/documents` | Upload a document (multipart) | User |
| GET | `/api/documents` | List documents | User |
| PATCH | `/api/documents/{document_id}` | Update document metadata | User |
| DELETE | `/api/documents/{document_id}` | Delete a document | User |
| GET | `/api/documents/{document_id}/chunks` | Preview a document's chunks | User |

## Folders

Folders organize chats. Names are path-style strings.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/folders` | List folders | User |
| POST | `/api/folders` | Create folders | User |
| PATCH | `/api/folders/{folder_name}` | Rename a folder | User |
| DELETE | `/api/folders/{folder_name}` | Delete a folder | User |

## Sharing

The share token route renders a read-only public view of a shared chat. It is the one user-facing route that is intentionally **unauthenticated** — anyone holding the token can read it (subject to expiry, revocation, and incognito rules). Share links are created and revoked via the chat endpoints above.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/share/{token}` | Public read-only view of a shared chat | Public |

## Models and lifecycle

Catalog listing plus model load, unload, download, and a manual catalog refresh. Listing and provider status are open to any user; the lifecycle operations and refresh are admin-gated.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/models` | List available models across providers | User |
| GET | `/api/providers/status` | Per-provider reachability and status | User |
| POST | `/api/models/load` | Load a model | **Admin** |
| POST | `/api/models/unload` | Unload a model | **Admin** |
| POST | `/api/models/download` | Download a model | **Admin** |
| POST | `/api/admin/models/refresh` | Refresh the model catalog | **Admin** |

## Parameters

Inspect which inference parameters a model rejected, and invalidate the cached rejection set.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/params/{model_id}` | Rejected-parameter set for a model | User |
| POST | `/api/admin/params/invalidate` | Invalidate the cached rejected-params set | **Admin** |

## Providers

Cloud-provider (OpenAI-compatible) configuration. All admin-gated; writes refresh the live registry and invalidate the catalog cache. The `provider` slug is taken from the URL path.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/admin/providers` | List provider configs (key-redacted views) | **Admin** |
| PUT | `/api/admin/providers/{provider}` | Add or update a provider config | **Admin** |
| DELETE | `/api/admin/providers/{provider}` | Delete a provider config | **Admin** |
| POST | `/api/admin/providers/{provider}/test` | Probe a provider's connectivity | **Admin** |

## Preset models

The model presets surfaced in the composer's quick picker.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/settings/preset-models` | Read the preset-model list | User |
| PUT | `/api/settings/preset-models` | Replace the preset-model list | User |

## LM Studio settings

Per-user LM Studio overrides (URL, API key, default model) plus admin-only connection probing and the server-wide default. The probe and admin-default routes are admin-gated and rate-limited because they make outbound HTTP requests to a caller-supplied URL (SSRF surface).

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/settings/lmstudio` | Resolved LM Studio config view | User |
| PUT | `/api/settings/lmstudio` | Patch the per-user override | User |
| GET | `/api/settings/lmstudio/env_suggestion` | Suggested env-var configuration | User |
| POST | `/api/settings/lmstudio/test` | Probe an LM Studio URL | **Admin** |
| PATCH | `/api/admin/lmstudio/default` | Set the server-wide default config | **Admin** |

## MCP store

Browse the integration catalog and install, configure, and inspect MCP servers. The entire MCP store surface is **admin-gated** — installing servers is operator work. For the user-facing in-chat tool picker, see the integrations group below.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/mcp-store/catalog` | Browse the MCP catalog | **Admin** |
| GET | `/api/mcp-store/servers` | List installed MCP servers | **Admin** |
| POST | `/api/mcp-store/servers` | Install an MCP server | **Admin** |
| PATCH | `/api/mcp-store/servers/{slug}` | Update an installed server | **Admin** |
| DELETE | `/api/mcp-store/servers/{slug}` | Uninstall a server | **Admin** |
| GET | `/api/mcp-store/servers/{slug}/tools` | Live tools for a server (denied-flagged) | **Admin** |

## Integrations

The list of available MCP integration IDs that populates the chat composer's tool picker. Reading is open to any user; setting the list is admin work.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/integrations/available` | List available integration IDs | User |
| PUT | `/api/integrations/available` | Replace the integrations list | **Admin** |

## Memory

Pinned insights (long-term memory) and the embedding/reindex pipeline. Pin management and refinement are user-level; reindexing the memory store is admin-gated.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| POST | `/api/memory/pin` | Pin a text insight | User |
| GET | `/api/memory/pins` | List pinned insights | User |
| DELETE | `/api/memory/pin/{insight_id}` | Unpin an insight | User |
| PATCH | `/api/memory/insights/{insight_id}` | Edit an insight | User |
| POST | `/api/memory/refine` | Refine an insight | User |
| POST | `/api/memory/restore/{history_id}` | Restore insights from history | User |
| GET | `/api/memory/embedding/status` | Embedding-model status | User |
| POST | `/api/memory/reindex` | Start a background reindex | **Admin** |
| GET | `/api/memory/reindex/status` | Poll reindex progress | **Admin** |

## Search

In-app search across the user's chats and messages, and standalone web search.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/search` | Search chats and messages | User |
| POST | `/api/search/web` | Web search | User |

## Prompts

The prompt library: reusable prompt entries scoped to the user.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/prompts` | List prompts | User |
| POST | `/api/prompts` | Create a prompt | User |
| PATCH | `/api/prompts/{prompt_id}` | Update a prompt | User |
| DELETE | `/api/prompts/{prompt_id}` | Delete a prompt | User |

## Quotas

Per-user usage limits. Users read their own quota; admins list and adjust all quotas.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/quotas/me` | Read the caller's quota | User |
| GET | `/api/admin/quotas` | List all quotas | **Admin** |
| PATCH | `/api/admin/quotas/{user_id}` | Update a user's quota | **Admin** |

## Analytics

Usage statistics. The personal view is user-level; the system aggregate is admin-only.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/analytics/me` | Personal usage statistics | User |
| GET | `/api/analytics/system` | System-wide aggregate statistics | **Admin** |

## Administration

Operator tooling: user management, role changes, session revocation, the audit log, invites, and a debug snapshot. Every route in this group is **admin-gated**.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/api/admin/users` | List users | **Admin** |
| GET | `/api/admin/users/count` | Count users | **Admin** |
| POST | `/api/admin/users/{user_id}/role` | Change a user's role | **Admin** |
| POST | `/api/admin/users/{user_id}/revoke-sessions` | Revoke a user's sessions | **Admin** |
| DELETE | `/api/admin/users/{user_id}` | Delete a user | **Admin** |
| POST | `/api/admin/invite` | Issue a one-shot invite token | **Admin** |
| GET | `/api/admin/audit-log` | Read the paginated audit log | **Admin** |
| GET | `/api/debug` | Diagnostic snapshot | **Admin** |

## Operational and shell endpoints

These are not part of the application API and are excluded from the OpenAPI schema. They support health checking, metrics, and serving the SPA itself.

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/healthz` | Liveness probe | Public |
| GET | `/readyz` | Readiness probe | Public |
| GET | `/api/metrics` | Process metrics | Public |
| GET | `/` | Serve the SPA shell | Public |
| GET | `/favicon.svg` | Favicon | Public |
| GET | `/manifest.webmanifest` | PWA manifest | Public |
| GET | `/sw.js` | Service worker | Public |

Unmatched browser navigations (for example `/chats/123` or `/settings`) fall through to the SPA shell with a `200` so the client-side router can render the right page. JSON-only callers receive the original `404` unchanged, so API clients are not confused by the SPA fallback.
