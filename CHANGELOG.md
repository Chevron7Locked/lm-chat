# Changelog

All notable changes to this project will be documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.4.11] — 2026-03-20

### Added
- `CHECKLIST.md` — authoritative living list of every feature, API integration point, and known gap
- SC/CoVe toggles added to global Settings → Chat tab (previously per-chat only); global defaults fall back from localStorage when no per-chat override is set
- Per-chat settings panel restructured: system prompt + temperature always visible; advanced settings collapsed under expander
- MCP servers now default to **on** — opt-out instead of opt-in; configured servers are active without manual toggling
- `LMSTUDIO_MCP_JSON` env var documented in README and Configuration table
- Accessibility: `aria-label` on all toggle checkboxes, focus trap + focus management for keyboard shortcuts modal, right panel `role="dialog"` with `aria-hidden` toggling and focus restore

### Fixed
- SSE upstream error delivery: `TypeError` (int + str) in error message formatting silently swallowed all non-400 upstream errors — 500s from LM Studio now correctly surface to the client
- PID file scoped by port (`_pid_file()` returns `.lm_chat_{PORT}.pid`) — prevents multi-instance cross-kills when dev and production servers share the same DB directory
- Slash command menu renders as overlay (absolute positioning) instead of pushing chat content up
- Auth logo glow reduced from 0.14 to 0.08 opacity (within CLAUDE.md's 0.04–0.12 cap)
- Auth/send button shadows normalized to `--shadow-md` / `--shadow-lg` tokens (no colored shadows)
- `msg-row` spacing uses `--sp-3` token instead of hardcoded `6px`
- Label `for="s-preset"` corrected from "System Prompt" to "Preset"
- Service worker: removed no-op `fetch` event handler (Chrome navigation overhead warning)

### Changed
- Border radius tokens bumped site-wide for rounder feel: `--r-sm` 0.375→0.5rem, `--r-md` 0.625→0.75rem, `--r-lg` 1→1.25rem, `--r-xl` 1.25→1.5rem
- Input box, chat search: full pill radius (`--r-full`)
- Sidebar chat items: `--r-lg` radius; active state uses darker `--surface` background
- Send button: 2.25rem → 1.75rem for breathing room inside pill input
- Spacing/subtext consistency improvements across settings panels
- README updated: architecture line counts, MCP behavior, Quality Modes section

## [0.4.2] — 2026-03-18

### Fixed
- `init_db()` crash on fresh install: `idx_shared_chats_chat_id` index was created before the `shared_chats` table existed
- `_kill_stale_server()` no longer kills unrelated processes on the same port (checks cmdline for `server.py` before sending SIGTERM)
- `hashlib.md5` ETag call annotated with `usedforsecurity=False` (not a security use; suppresses scanner false positive)
- Test suite: 9 tests used `POST` on `PATCH`-only endpoints (`/api/chats/{id}/title`, `/api/auth/profile`, `/api/insights/settings`)

### Changed
- Design system: message action buttons fade in on hover (always visible on touch); message density increased; active sidebar item uses `--surface-raised`; typography floor enforced at `--text-xs` (12px) across all form labels and secondary text; LM Studio brand purple corrected throughout (`rgba(192,132,252,…)` replacing stale indigo values)
- Form fields standardised: all inputs/selects now use `--text-base` (14px), `--sp-4/--sp-5` padding, `--r-md` radius, and `transition: border-color/box-shadow` — eliminates the size jump between settings panel and password-change fields
- Markdown heading hierarchy restored: `h1`→`--text-2xl`, `h2`→`--text-xl`, `h3`→`--text-lg`
- Right panel width made fluid (`clamp(16rem, 14rem + 2vw, 20rem)`) and transition uses `var(--ease)`
- Status indicator dots: colored `box-shadow` glows removed (invisible on Windows, inconsistent cross-platform)

## [0.3.1] — 2026-03-17

### Added
- Syntax highlighting via vendored highlight.js — no external CDN dependency

### Fixed
- Distillation concurrency guard per chat (threading.Lock + in-flight set)
- Response ID forwarded into forked chat when forking at last message
- Right panel moved into `@layer components` with mobile breakpoint
- `user-dd` settings selector uses `:first-of-type` (first child is a div, not a button)
- Route method-map hoisted to class constant; unknown methods return 405

### Changed
- All inline `onclick`/`onchange` handlers in HTML moved to `addEventListener` in `initEventHandlers()`
- `style-src 'unsafe-inline'` retained (dynamic `style=` attributes in JS templates)

## [0.3.0] — 2026-03-16

### Added
- Message pinning: pin individual assistant responses, browse all pins globally, pins survive `/compact`
- Response feedback: thumbs up/down per message with optimistic UI
- Per-chat settings panel: inference overrides (temp, top_p, reasoning depth, etc.) per conversation
- Bayesian Laplace scoring for memory insight retrieval weighted by feedback

### Changed
- Routing refactored from `elif` chains to declarative `_GET_ROUTES` / `_POST_ROUTES` / `_DELETE_ROUTES` / `_PATCH_ROUTES` class constants
- SC/CoVe status events streamed to UI during processing

## [0.2.2] — 2026-03-16

### Fixed
- `/compact` error handling and timeout configuration
- `{{tools}}` template variable returns empty string when no MCP tools loaded (avoids redundant context tokens)
- System prompts rewritten per 2026 prompting best practices

## [0.2.1] — 2026-03-15

### Fixed
- Model routing uses instance ID to prevent JIT reloads on hot-swap
- CI: spelling check across all source files, smoke test for all static routes

## [0.2.0] — 2026-03-15

### Added
- Presence penalty slider
- Context length display (read-only, load-time parameter)
- SameSite=Strict on all session cookies
- requestAnimationFrame for streaming markdown rendering (replaces setInterval)
- content-visibility on chat messages (off-screen rendering skip)
- CSS @layer architecture: reset → tokens → base → layout → components → utilities
- ETag caching for static files (304 Not Modified)

### Changed
- JS extracted to `app.js`, CSS extracted to `style.css` (was inline in HTML)
- CSS converted to native nesting (261 selectors)
- SQLite: synchronous=NORMAL, 64MB cache

### Fixed
- Multiple refactors for DRY (insight parsing, user-filter queries, _lmstudio_chat helper)
- Socket timeout + bounded thread pool (max 64 workers)
- Declarative migration list (replaces 9 try/except blocks)

## [0.1.5] — 2026-03-13

### Fixed
- Auto-kill stale server process on startup (prevents port conflicts)

## [0.1.4] — 2026-03-13

### Fixed
- Docker: bind mounts instead of named volumes (prevents data loss on container recreate)

## [0.1.3] — 2026-03-13

### Added
- Per-user API key storage (encrypted at rest, per-user LM Studio authentication)
- Adaptive memory: auto-distillation, cognitive decay scoring, category-weighted injection
- Semantic search via embedding model (falls back to text search if unavailable)
- Remote MCP server support (URL + optional auth token)
- Agent mode presets: `/research`, `/code`, `/write`, `/analyze`, `/architect`, `/custom`
- Chat export (Markdown, JSON)
- Conversation sharing (read-only links with CSP sandbox)
- TOTP 2FA (RFC 6238, stdlib QR generator)
- Multi-user auth: invite system, admin user management, per-user chat isolation
- PWA support (manifest, service worker, apple-mobile-web-app-capable)
- `/compact` command (LLM-summarized context window management)
- Incognito mode (no persistence, no memory injection)
- Keyboard shortcuts (Cmd+N, Cmd+,, Cmd+Shift+S, Cmd+Shift+E, Esc)

### Fixed
- Response ID chaining (LM Studio native API `previous_response_id`)
- Multiple visual polish passes (depth, micro-interactions, cross-platform color system)

## [0.1.2] — 2026-03-12

### Added
- TOTP partial-token flow for 2FA login
- Scrypt password hashing with timing-safe comparison
- CSRF protection (custom header validation)
- Rate limiting (login attempts, per-user API calls)

## [0.1.1] — 2026-03-12

### Added
- CI pipeline: Ruff lint, Bandit security, CodeQL SAST, Dependabot
- Docker image security hardening (non-root user, read-only root, tmpfs, no-new-privileges)
- Health check in Dockerfile

## [0.1.0] — 2026-03-10

Initial release.

- Zero-dependency web UI for LM Studio (stdlib Python, vanilla JS)
- LM Studio native API integration (`/api/v1/chat` with `response_id` chaining)
- MCP tool execution (agentic loops, tool call display)
- Reasoning display (collapsible thinking blocks)
- Streaming with token stats (TTFT, tokens/sec)
- Dark theme tuned for LM Studio desktop palette
- Mobile-responsive with PWA support
- SQLite persistence (WAL mode)
- Rotating file logs (5 × 5 MB)
