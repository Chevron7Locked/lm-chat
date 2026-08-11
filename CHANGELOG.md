# Changelog

All notable changes to LM Chat are documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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

## 1.0.0

Initial public release.

LM Chat is a self-hosted, browser-native chat client for LM Studio and any
OpenAI-compatible provider (OpenAI, OpenRouter, Groq). It adds persistent
conversations, adaptive long-term memory, projects with a document knowledge
base (RAG), six personas plus sub-agent modes, a one-click MCP server store,
A/B model compare, per-user auth with TOTP, and a full admin surface.

Licensed under Apache-2.0.
