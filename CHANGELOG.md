# Changelog

All notable changes to LM Chat are documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Tags and archiving for chats: tag a chat, and archive or unarchive it from
  the sidebar. Archived chats are hidden from the default list without being
  deleted.
- "Fork from here" on any assistant message branches a new chat at that
  point — the `/fork` command is no longer the only way in.
- Queue a message while a response is still streaming: the composer stays
  open and typable during a stream, a message you send mid-stream queues
  (FIFO, with a visible pending row) and auto-sends once the turn finishes,
  and stopping a stream keeps the queued draft instead of discarding it.
- A reversible full-screen focus mode hides the sidebar and top chrome down
  to a single reading column. Toggle it from the TopBar, with Cmd/Ctrl+.,
  or from the mobile overflow menu; Esc or the always-reachable "Exit
  focus" pill leaves it.
- The tool-call repeat-loop cutoff (K) is now an adjustable setting, both
  globally (admin) and per chat, instead of only an environment variable.
- RAG: an optional diversity re-rank (MMR) over retrieved document chunks,
  on by default. It removes near-duplicate chunks that plain keyword+vector
  fusion could otherwise return twice while burying a distinct one.
- Durable sub-agent sessions: `/research`, `/code`, `/write`, `/analyze`,
  `/architect`, and role sub-sessions now persist to the database instead of
  living only in the browser tab. A reload no longer loses an in-progress or
  just-finished sub-session; a "Sub-session history" menu lists past runs,
  and you can reopen and continue any of them.
- Model-driven mode adoption, **opt-in and off by default**: with
  `LM_CHAT_MODE_ADOPTION_ENABLED=1` set, the model can adopt one of the six
  personas for its own next turn, instead of only being able to suggest a
  slash command for you to run yourself.

### Changed

- The model picker shows "Auto" instead of a specific model name when a
  chat has no per-chat override; it still resolves to your configured
  default, and picking "Auto" explicitly clears any override.
- The tool-call repeat-loop cutoff default was raised from 2 to 16
  identical calls, so multi-step research and agentic turns stop hitting a
  false "stuck in a loop" cutoff early.
- Sub-agent output injected back into the main chat is now capped at 8,000
  characters by default (configurable), with a clear truncation marker,
  instead of being unbounded.
- Memory recall's similarity search is faster on large memory stores;
  ranking and scores are unchanged.

### Fixed

- Reasoning and tool calls are no longer lost when a connection drops
  mid-answer or a turn is interrupted — both now persist while the turn is
  still running, not only once it completes.
- Sub-session panel: fixed a race that could leave it stuck on "Generating
  summary…" after a turn had actually already finished. A stalled model can
  also no longer hang a sub-session turn indefinitely — it now times out and
  salvages the partial answer, the same as the main chat does.
- A single corrupted message row no longer fails the entire chat load; the
  bad row is repaired or skipped and the rest of the conversation loads
  normally.
- Per-chat overrides (system prompt, reasoning effort, active persona,
  sampler settings) can be cleared back to "inherit the default" again —
  clearing one had silently no-op'd, or, for numeric fields, returned an
  error.
- The model is now told when a tool it was offered didn't actually reach
  the request (dropped for context-budget or capability reasons), reducing
  cases where it answers with a fake tool call written out as text.

### Security

- Hardened the SearXNG web-search SSRF guard against DNS-rebinding and
  alternate IP-address-encoding bypasses that could otherwise reach internal
  network addresses.

## 1.0.2

### Added

- `LM_CHAT_LMSTUDIO_MCP_CONFIG_PATH` lets you point LM Chat at a
  non-default location for LM Studio's `mcp.json`.
- Startup now logs a clear warning, with the path it checked and how to fix
  it, if local MCP discovery is enabled but finds no servers, instead of
  silently returning none.

### Fixed

- The Docker healthcheck no longer reports the container "unhealthy" right
  after startup.
- Web search's fallback provider now defaults to DuckDuckGo instead of the
  now-dead public `searx.be`.
- Login now defaults to allowing multiple concurrent sessions, so signing
  in on a second device no longer signs you out of the first.

## 1.0.1

Maintenance release: dependency floor bumps (gitpython, js-yaml, nanoid) to
clear a security scan. None of the affected packages ship in the built
application, so there is no user-facing change.

## 1.0.0

Initial public release.

LM Chat is a self-hosted, browser-native chat client for LM Studio and any
OpenAI-compatible provider (OpenAI, OpenRouter, Groq). It adds persistent
conversations, adaptive long-term memory, projects with a document knowledge
base (RAG), six personas plus sub-agent modes, a one-click MCP server store,
A/B model compare, per-user auth with TOTP, and a full admin surface.

Licensed under Apache-2.0.

### Added

- Crawl4AI, a self-hosted web-scraper MCP server, needs no vendor API key.
- MCP Store catalog expanded from 10 to 23 servers.

### Changed

- MCP integrations are no longer enabled by default on a fresh install.
  Servers you configure in LM Studio are still discovered and available, but
  new chats start with no tools armed — you opt in per chat. Admins can
  restore pre-selection via the integrations setting.
- `/compact` is now hybrid compaction: it summarizes the oldest messages
  with a local model and archives them into a foldable recall tab instead
  of trimming or deleting them. Nothing is lost — the summary is injected
  into context and the originals stay one click away.
- `/readyz` no longer reports not-ready when LM Studio is unreachable —
  readiness now reflects only the database and session store. LM Studio is
  still reported (as `degraded`) but closing the desktop app no longer makes
  an orchestrator pull the whole app from rotation.

### Fixed

- RAG: project documents were being truncated to roughly a quarter of
  their content before injection; the full document now fits within the
  token budget, so project chats can see the whole thing.
- Auto-memory: newly learned facts could fail to surface, or be evicted
  before older ones, and merging near-duplicate insights could drop a
  distinct fact. Recall is reliable now.
- `/compact` now summarizes and reorders multi-part histories (ones with
  interleaved tool calls) correctly.
- Web search now falls back to DuckDuckGo when SearXNG is unreachable
  instead of erroring.
- A/B compare now errors clearly when a pane's model has been unloaded,
  instead of silently comparing a model to itself.
- Quota, analytics, and project dates now use UTC consistently, fixing
  an off-by-a-day on non-UTC hosts.
- Re-uploading a file into a different project now keeps it separate
  from the original instead of merging them.
- LM Studio tool-pipeline: a tool whose schema fails LM Studio's grammar
  now degrades gracefully with a warning instead of failing silently.
- A chat that requested an MCP integration LM Studio couldn't serve (for
  example a server that was removed or renamed) used to fail the whole turn
  with no reply; it now drops just the unavailable tool and answers.
- Auto-generated chat titles no longer occasionally store the
  title-generation instruction itself as the title.
- Settings: tightened the navigation spacing, and the mobile section picker
  is now a native dropdown instead of a slide-in panel.

### Security

- Updated pypdf to 6.15.0 to fix a crafted-PDF denial-of-service
  (CVE-2026-71852 / CVE-2026-71870) reachable through document upload, and
  added a floor for h2 4.4.1 (CVE-2026-71554).
