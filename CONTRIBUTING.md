# Contributing to LM Chat

Contributions are welcome. LM Chat is a FastAPI backend (`src/lmchat`) with a
React/Vite frontend (`web/`), meant to run locally against LM Studio.

## Setup

Backend (Python 3.11+, using [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/Chevron7Locked/lm-chat.git
cd lm-chat
uv sync
uv run uvicorn lmchat.app:app --port 8011 --reload
```

Frontend (Node 22+, using [pnpm](https://pnpm.io/)):

```bash
cd web
pnpm install
pnpm dev            # proxies /api to the backend on :8011
```

The frontend's API types (`web/src/types/api.ts`) are generated from the
backend's OpenAPI spec. They're committed, but if you change a backend route,
regenerate them: `make emit-openapi && (cd web && pnpm codegen)`.

## Before you open a PR

Run these locally:

```bash
make gates          # ruff + pyright + pytest (≥75% cov) + doc/spec checks, AND
                     # the routine frontend suite (web/ typecheck + lint + vitest)
make security-scan  # bandit + pip-audit + secret scan
```

Both must be green. `make gates` runs the frontend suite via `make
web-suite` — run `make web-suite` on its own for a fast frontend-only check
while iterating. `make web-gates` (`web-suite` plus a build and the offline
Playwright suite across 4 browsers) is heavier and not required per-PR — CI
already runs it on PRs; run it locally when you've touched a user-facing
flow and want to see it before pushing.

New behaviour needs a test; a fix needs a regression test that fails on the
un-fixed code.

## Sign-off (DCO)

Commits must be signed off under the [Developer Certificate of
Origin](https://developercertificate.org/) — add `-s` to your commit:

```bash
git commit -s -m "your message"
```

This appends a `Signed-off-by:` line certifying you wrote the change (or have
the right to submit it). CI enforces it.

## Conventions

- Match the style of the surrounding code; `ruff`, `pyright`, and `eslint` are
  the arbiters.
- Keep the change focused. Unrelated cleanups go in their own PR.
- License is Apache-2.0 (`web/`, `src/`); by contributing you agree your work
  ships under it.

Thanks for helping.
