# Changelog

All notable changes to this project will be documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.5.8] — 2026-05-18

### Fixed
- **Image attachments now reach the model.**  The SPA was building text
  parts as ``{type: "text", text: "..."}`` (OpenAI-compat shape), but
  LM Studio's native ``/api/v1/chat`` rejects that with
  ``'input.0.content' is required, Unrecognized key(s) in object: 'text'``.
  The user-visible symptom was: attach an image, ask a question, the
  request 200s through lm-chat, then a red error banner appears with
  the LM Studio 400.  Fix is in two layers — SPA emits the canonical
  ``content`` field, and ``server.py:_normalize_input`` rewrites any
  surviving ``text`` keys at the wire boundary so future regressions
  on either side land safely.  Probed against live LM Studio to
  confirm the accepted shape before committing.  14-test backend
  regression suite in ``tests/test_normalize_input.py`` locks the
  contract.
- ``.github/workflows/ci.yml`` — pinned ``jakebailey/pyright-action``
  to the existing tag ``v3.0.2`` (was ``1.0.4`` which doesn't exist;
  CI failed at the action-resolution step on every push since 0.5.6).

## [0.5.7] — 2026-05-17

Pre-rebuild perimeter hardening.  Four items lifted directly from the original
strategic-acquisition reviews (Opus + Qwen first pass) that were real, small,
and locally fixable without waiting on the 0.6.0+ canonical-spec rebuild.
Two other strategic-review items (`/api/debug` admin gate; `_handle_chat_stream`
rate limiter) turned out to already be implemented — false alarms on both
reviewers, noted and verified before shipping.

### Security
- **TOTP shared secret is now encrypted at rest.** Previously stored in
  plaintext at `users.totp_secret`; an attacker with read-only DB access
  could clone every enrolled user's 2FA token generator.  Write site
  (`server.py` TOTP-confirm) and the three read sites (disable, login,
  re-enrollment) all go through `encrypt_at_rest` / `decrypt_at_rest`
  with context `"users.totp_secret"`.  Legacy plaintext rows continue to
  verify until the user re-enrolls — `decrypt_at_rest` returns input
  unchanged when no `enc$v1$` prefix is present.
- **`LM_CHAT_SINGLE_SESSION` now defaults ON.** Login rotates the user's
  other sessions atomically (ASVS V3.3.3 — session re-id on privilege
  elevation).  Operators who want multi-device simultaneous sessions opt
  out with `LM_CHAT_SINGLE_SESSION=false`.  Treats unset as the secure
  default.
- **`X-Forwarded-Proto` is no longer blindly trusted.**  A client behind
  plain HTTP could previously send `X-Forwarded-Proto: https` and trick
  the server into emitting a `Secure`-flagged `__Host-` cookie that the
  browser would never return — effectively a session-loss DoS or, worse,
  a session-fixation primitive.  Now gated behind a new
  `LM_CHAT_TRUSTED_PROXY` env, set to `true` only when an actual reverse
  proxy is in front.
- **Per-IP rate limiter no longer trusts spoofable headers by default.**
  New `_client_ip()` helper centralizes client-IP resolution and honors
  `X-Forwarded-For` / `X-Real-IP` only when `LM_CHAT_TRUSTED_PROXY` is
  set.  Four call sites updated: login, setup, 2FA login, request log
  line.

### Docs
- README architecture stats refreshed to match the current tree
  (`server.py` ~5.4k, `index.html` ~685, `style.css` ~3.7k, `app.js`
  ~7.5k) — previous values predated the 0.5.x feature waves.

### Out of scope (deferred to canonical-spec rebuild)
- Audit-log table + admin viewer.  Tracked in `ARCHITECTURE_SPEC_canonical.md`
  Wave 0 security stream.
- CSP nonce + secret-key rotation.  Same wave.
- Async conversion of `_handle_chat_stream` — blocker for the streaming
  refactor in Wave 1; not patchable in a point release.

## [0.5.6] — 2026-05-17

Pre-existings polish + CI alignment.  Eight-item plan reviewed adversarially
by qwen 3.6 at full 262144 context — review caught four real bugs in
my own plan, all incorporated below.

### Fixed
- `_proxy_get` and `_get_models` callers of `_proxy_lmstudio` now guard
  against both `None` AND empty-bytes (`b""`) bodies.  Previously the
  pyright-flagged "could be None" path was the obvious one; reviewer
  caught that a 200 from upstream with an empty body would land the SPA
  in `JSON.parse("")` territory.  Single guard `if not body:` covers
  both at each call site.
- CI coverage flag drift: `.github/workflows/ci.yml`'s "Coverage report"
  step ran `coverage report --fail-under=40` while `pyproject.toml` set
  `fail_under = 68`.  CLI flag wins, so CI passed at 41% while local
  pytest failed at 69%.  Both now set to **70** with an explicit
  "keep in sync" comment in both files.

### Added
- `pyrightconfig.json` — first project-level pyright config.  Sets
  `typeCheckingMode: basic`, excludes `.playwright-mcp` + `.coverage*`
  scratch dirs, silences `reportUnusedParameter` and
  `reportUnusedLambdaParameter` (both are universally noisy in this
  codebase: `log_request(code, size)` must keep `size` to override
  `BaseHTTPRequestHandler`, and every route lambda has signature
  `(s, m, b)` per the dispatcher contract regardless of which args
  it reads).  Rationale comment lives inside the JSON.
- CI pyright step (`.github/workflows/ci.yml`): `jakebailey/pyright-action`
  runs after Ruff so type-check status can't drift between local dev and
  CI.

### Changed
- `.gitignore`: added `.playwright-mcp/` so the browser-MCP scratch dir
  (90+ session screenshots / console logs) stops cluttering `git status`.
- `pyproject.toml` coverage `fail_under` 68 → 70 (matches actual measured
  coverage of 73% with the documented 3-point jitter buffer).
- VERSION constant (`server.py:14`) bumped 0.5.5 → 0.5.6.

### Out of scope (tracked separately)
- 5 Dependabot PRs (#10-#14): every action version they propose is
  **already in main** (verified: upload-artifact@v7, hadolint@v3.3.0,
  scorecard@v2.4.3, action-gh-release@v3, python:3.14-slim).  Stale
  PRs are being closed with an "already applied" comment.
- `_handle_chat_stream` (server.py:2643-5221, 2578 lines) split → 0.6.0.
  Qwen's review surfaced a hidden hazard: the retry-with-strip block at
  server.py:2850-2925 uses synchronous `_ready2.wait(timeout=300)` on
  the stream writer thread.  Splitting helpers without converting to
  asyncio or a bounded queue could deadlock under high-latency upstream
  retries.  0.6.0 scope must include the async conversion as a
  prerequisite.

## [0.5.5] — 2026-05-17

Mobile + PWA UX pass.  Five reproducible bugs on iPhone-class viewports.

### Fixed
- **Topbar overlaid the sidebar on mobile.**  `#topbar` had `z-index: 200`,
  `#sidebar` had `z-index: 95`.  Opening the sidebar on a phone left the
  topbar buttons (Export, Pin, Settings, User-avatar, model picker)
  rendering ON TOP of the sidebar's New-chat button, search box, and
  pinned-chats list.  Users had to refresh the tab to escape.  Sidebar
  z-index bumped to 250 inside the `@media (max-width: 768px)` block so
  it floats above the topbar when open.
- **Sidebar opened by default on mobile.**  `app.js:1` only removed
  `sb-closed` on desktop, never added it on mobile.  Initial `<body>`
  had no class, so the full-viewport sidebar covered the chat from
  first paint.  Combined with bug #1, users couldn't see or use the
  chat at all on a cold mobile load.  Now `else document.body.classList.add("sb-closed")`
  on mobile init.
- **Input area lost the safe-area-inset on mobile.**  Desktop
  `#input-area` set `padding-bottom: max(var(--sp-6), var(--sab))` so
  the iPhone home-indicator gesture area was respected.  Mobile's
  all-sides `padding: var(--sp-3) var(--sp-5)` shorthand dropped that.
  Now explicit `padding-bottom: max(var(--sp-3), var(--sab))` restores
  the inset on iOS standalone PWA.
- **Reasoning-effort cycle button orphaned visually on mobile.**  Lived
  inside `#input-box` but rendered as a separate visual element to the
  left of the rounded input pill, making the input area feel
  disjointed.  Hidden on mobile — users set reasoning effort via the
  chat-settings panel.  Desktop unchanged.
- **No close affordance reachable on mobile sidebar.**  The hamburger
  toggle (`#open-sb`) lives in the topbar.  Before bug #1's fix, the
  topbar overlay covered "New chat" AND there was no visible close
  control — refresh was the only escape.  Now resolved by the z-index
  fix above; the sidebar's own close X (`#close-sb`) is tappable.

### Changed
- VERSION constant (`server.py:14`) bumped 0.5.4 → 0.5.5.

## [0.5.4] — 2026-05-17

Hot-fix for an indicator-regression introduced during the 0.5.1 polish
pass.

### Fixed
- "Thinking..." indicator never appeared after the user visited Settings
  once.  `openSettings` set `$("thinking").style.display = "none"` as an
  inline style (added during the 0.5.1 scroll-bottom polish pass), and
  `closeSettings` never restored it.  Inline `display: none` shadows the
  `#thinking.on { display: flex }` CSS rule indefinitely — so subsequent
  message sends would call `classList.add("on")` with no visible effect.
  The chat just appeared to hang silently until the first stream token
  arrived.  `closeSettings` now restores `style.display = ""` alongside
  the other elements it un-hides.

### Changed
- VERSION constant (`server.py:14`) bumped 0.5.3 → 0.5.4.

## [0.5.3] — 2026-05-16

Hot-fix for a 0.5.2 oversight discovered live: Self-Consistency and
Chain of Verification both stripped `integrations: []` on their internal
calls, including the candidate-generation step (SC) and the draft step
(CoVe).  When a user enabled SC or CoVe with tools configured, the
generation calls went out tool-less, the model produced hallucinated
"I don't have access to that tool" responses, and CoVe then fact-checked
the hallucinated draft.

### Fixed
- `_self_consistency` now generates each candidate with the user's
  original `integrations` array; only the final synthesis call stays
  tool-less (it's combining text outputs, not generating fresh ones).
- `_chain_of_verification` Step 1 (draft) now uses the user's
  integrations so tools actually fire on the user's question.  Steps 2-4
  (verification question extraction, parallel VQ answers, synthesis)
  remain tool-less by design — they fact-check the draft's claims from
  the model's training knowledge, no tool calls needed.
- Verified live with `filesystem` MCP + CoVe enabled: model returned
  `ref: refs/heads/main` verbatim from reading `.git/HEAD`, a value
  it could not have hallucinated.

### Changed
- VERSION constant (`server.py:14`) bumped 0.5.2 → 0.5.3.

## [0.5.2] — 2026-05-16

Five field-discovered bugs from a follow-on audit pass against the 0.5.1
working install.  All five surface in real-world deployments — most prominently
the Docker / remote-LM-Studio case — but had silent failure modes that the
prior tests never caught.

### Fixed
- **Docker installs never discovered MCP tools.** Container `$HOME` is `/app` (Dockerfile `useradd -d /app`), so `_list_mcp_servers` was reading `/app/.lmstudio/mcp.json` — a path that doesn't exist in the image and was never bind-mounted by the bundled compose files.  Dockerfile now defaults `LMSTUDIO_MCP_JSON=/lmstudio/mcp.json`; both `docker-compose.yml` and `docker-compose.dev.yml` bind-mount `${HOME}/.lmstudio/mcp.json:/lmstudio/mcp.json:ro` so same-host users get plugin auto-discovery out of the box.  Remote-LM-Studio deployments override the volume line to point at an SSHFS / rsync'd / copied path.
- **Chain of Verification silently degraded to a single-call draft.** `_lmstudio_chat` was a thin urllib shim that bypassed the universal `_unsupported_params` cache (the strip lived only in `_build_lmstudio_payload`).  CoVe hardcodes `"reasoning": "off"` in its base payload, which qwen3.6 and other intrinsic-thinking models reject with HTTP 400 — the pipeline aborted after Step 1 (draft), the exception was caught at `_handle_chat_stream:2716-2718`, and the user got the bare draft.  Paid 4× tokens (per the README claim), got 1× output, no warning.  `_lmstudio_chat` now mirrors the existing strip-up-front + harvest-on-400 + retry-loop pattern from `_handle_chat_stream:2851-2925`.  All seven direct callers (CoVe ×4, title gen, `/compact`, distill, `_handle_chat` non-stream) get the same protection.
- **Reasoning cycle button stayed clickable on models that reject the param.** The SPA's `PARAM_CONTROLS` map at `app.js:1800` listed the form-style dropdowns (`s-reasoning`, `cs-reasoning`) but not the input-area cycle button (`reasoning-btn`).  Comment at line 1864 claimed the server's proactive blacklist "governs whether the button is DISABLED entirely" — but nothing actually disabled it.  Added `reasoning-btn` to the list; `renderReasoningUI` was already wired to set the right tooltip when `btn.disabled`.
- **Semantic search never indexed normal-completed chats.** The `_index_embeddings` thread spawn at `_handle_chat_stream:3011-3034` was nested inside the `elif chat_id and not is_incognito:` interrupted-stream cleanup branch — indentation drift from an earlier refactor.  Normal completions (the 99% case) never spawned the indexer, the embeddings table stayed empty in production, and `_search_messages` silently fell back to LIKE matching despite the README claiming "search by meaning across all messages."  Dedented the block and wrapped it in a sibling `if chat_id and not is_incognito:` guard so it fires for both completion paths.
- **Semantic search fall-back was indistinguishable from text-mode-by-choice.** When `_search_messages` couldn't reach an embedding model, it called `_search_messages_like` which returned `{"mode": "text"}` — the same shape as a deliberate text-mode search.  The SPA had no way to know its semantic query had degraded.  Server now returns `{"mode": "text_fallback", "reason": "..."}` from the two semantic fallback paths, and `app.js:2582-2604` surfaces a one-time `toast.warn` so the user knows to load an embedding model in LM Studio.

### Changed
- VERSION constant (`server.py:14`) bumped 0.5.1 → 0.5.2.
- Cloud-cost baseline updated from GPT-5.2 ($1.75/$14 per 1M tokens) to GPT-5.5 Pro ($30/$180 per 1M tokens, released 2026-04-24).  The sidebar "$ saved vs cloud" counter now compares local inference against the most expensive current cloud SKU.  Comment moved alongside the constants with a verified date.

## [0.5.1] — 2026-05-16

Memory v3 + frontend polish + LM Studio robustness.  No schema breaks;
old embedding rows are rebuilt lazily on next retrieval; old chats
render unchanged.

### Added
- Memory v3: per-row `embedding_model_id` + 60s-TTL `_current_embedding_model_id()`.  Retrieval skips rows whose model id no longer matches active model — cosine similarity is only meaningful within a single embedding model.  Backfill is atomic (one transaction) and idempotent.
- Memory v3: `pinned` boolean on `user_insights` with a global `_PINNED_INSIGHTS_LIMIT = 5` cap.  Pinned rows always surface ahead of retrieval; unpinning falls back to similarity ranking.  Server enforces the cap; client UI exposes a star toggle in the right-panel pins view.
- Memory v3: MD5 content-hash dedup (Mem0 precedent) — `_content_hash_text()` normalizes whitespace + case and writes `user_insights.content_hash`.  Re-emitting the same insight is a no-op instead of a duplicate row.
- Memory v3: `POST /api/memory/reindex` — atomic per-user re-embed.  Used after a model swap to rebuild stale rows up front instead of waiting for lazy refresh.
- Universal per-model rejected-param cache (already on `main` as `940b402`) — persisted in SQLite (`unsupported_params` table), keyed `(model_id, param_name)`.  Replaces the retry-every-turn loop with proactive skip on subsequent payloads.
- SPA route 404 fallback — `/chat/<id>` on hard refresh now serves `index.html` instead of `404`, so deep links and back/forward navigation always land in the SPA.
- E2E test suite for the 9-phase refactor: `tests/test_e2e_refactor.py` exercises the state machine, declarative renders, routing, toasts, orphan-stream recovery, and outside-click hardening end-to-end.

### Fixed
- Markdown renderer (`app.js: md()`) was running bold/italic regexes on code-fence content, so ``**kwargs`` inside Python blocks rendered `<strong>kwargs</strong>` inside `<code>` — highlight.js then emitted "unescaped HTML" warnings on every chat load.  Code fences and inline code are now extracted to placeholders before `esc()` and restored after all other transforms.  Zero hljs warnings in console.
- Right-panel (chat-settings / pins) overlay used to clip chat content under the panel.  Adding `body.has-right-panel` reflows `main` with `padding-right: clamp(16rem, 14rem + 2vw, 20rem)`; mobile keeps full-width overlay.
- Admin user list exposed the synthetic `default` user (the auth-disabled-mode FK-integrity placeholder) with a `Delete` button.  `_auth_list_users` now filters `WHERE username != 'default'`.
- Settings panel showed the floating `scroll-to-bottom` arrow over its content.  `openSettings` / `closeSettings` now toggle that button's display alongside the rest of the chat UI.
- User-avatar dropdown sticky-reopened after clicking Settings / Sign Out because the inner button's click bubbled up to the avatar's toggle handler.  Click handler now checks `e.target === userAvatar`.
- Toast `text` + `detail` rendered side-by-side because `.toast` was row flex with `text` + `detail` as sibling children.  `.toast` is now column flex with absolute-positioned close button.
- `_current_embedding_model_id()` was being called on the request hot path on every memory retrieval.  60s-TTL cache eliminates the per-request LM Studio probe.

### Changed
- VERSION constant (`server.py:14`) bumped 0.5.0 → 0.5.1.
- `_list_insights` SELECT now returns `pinned` in its result mapping (required by the new pins UI; 11 of 17 memory tests were keying on it).



Security-focused release.  Several findings from a code review were fixed in
a single bundle; the at-rest format for stored secrets changed (existing DBs
transparently upgrade on next write) and ``LM_CHAT_AUTH=false`` mode is now
strictly single-user.  Backwards compatible for all data and HTTP routes —
no client-side action required.

### Added
- Test architecture: in-process server fixtures (`tests/conftest.py: inproc_server`, `make_inproc_server`) so handler branches are reachable to coverage tracing.  Subprocess coverage capture (`tests/sitecustomize.py`) fixed for Homebrew Python.  `pyproject.toml fail_under` raised 40 → 68.
- SSE error-frame contract tests (`tests/test_sse_error_frames.py`) including a source-level invariant that prevents future data-only frames from being added.
- `LM_CHAT_HSTS` (and `LM_CHAT_HSTS_MAX_AGE`) env vars — opt-in HSTS header, only emitted when serving via HTTPS (LM_CHAT_HTTPS or X-Forwarded-Proto).  Accepts `true`/`1`/`on`/`preload`.
- `LM_CHAT_SINGLE_SESSION` env — when set, login revokes all of the user's other sessions atomically.  Default unchanged (multi-device sessions allowed).
- `LM_CHAT_SETUP_TOKEN` env — when set, `/api/auth/setup` requires the matching token in `body["setup_token"]`.  Closes the first-visitor-wins admin window on public URLs.
- `LM_CHAT_SCRYPT_N` / `_R` / `_P` env vars — operator-tunable scrypt cost parameters.
- Composite password-hash format `scrypt$n=N$r=R$p=P$<hex>`.  Each hash now carries its own cost; legacy bare-hex hashes still verify against their original parameters.
- At-rest encryption for `user_settings.lm_apikey` and `user_settings.remote_mcps` — stdlib-only authenticated encryption (SHAKE-256 stream cipher + HMAC-SHA256, keys derived via HKDF from `LM_CHAT_SECRET`).  Format `enc$v1$<base64>`.  Legacy plaintext rows still readable and transparently upgraded on next write.
- Startup safety gate: refuses to start with `LM_CHAT_AUTH=false` when the DB already contains non-default users (would otherwise expose every user's data to every visitor).

### Fixed
- SSE error frames at `_handle_chat_stream` (server.py around lines 1957 and 1967) emitted `data:` without an `event:` line — `app.js:processSSEBlock` silently dropped them, so users saw a dead spinner on "no response from model" and on stream-collect exceptions.  Now both paths emit `event: error`.
- Password hashing: scrypt cost bumped from `n=16384` (OWASP 2017 floor) to `n=131072` (OWASP 2024).  Existing hashes verify against their original parameters and are silently upgraded on the next successful login.  scrypt `maxmem` ceiling raised to accommodate the new cost.
- Password length: capped at 256 chars before scrypt.  Prevents a 50 MB-password DoS via `/api/auth/login` and `/api/auth/setup`.
- Chat ownership: `_verify_chat_owner` and `_user_filter` no longer skip the user-id check when auth is disabled.  Combined with the startup gate above, this closes the cross-user leak that surfaced when an operator toggled auth off on a populated multi-user DB.
- VERSION constant (server.py:14) was stuck at `"0.4.10"` while three subsequent tags shipped; now `"0.5.0"`.

### Changed
- Test suite: 80 new security-focused tests (password cap, HSTS, session rotation, setup token, scrypt cost, chat ownership, at-rest encryption, SSE contracts).  Total: 279 passing, 1 xfail.  Server.py coverage 0% → 72%.
- README + SECURITY.md (next): document the four new env vars and the new at-rest encryption story.

### Removed
- `detectModelFamily()` (app.js) — unused since at least 0.3.x.
- `DEFAULT_INTEGRATIONS` constant (server.py) — empty-list sentinel inlined at its three call sites.

## [0.4.11] — 2026-03-20

### Added
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
