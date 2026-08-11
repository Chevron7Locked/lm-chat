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
make gates          # backend: ruff + pyright + pytest (≥75% cov) + doc/spec checks
make security-scan  # bandit + pip-audit + secret scan
cd web && pnpm typecheck && pnpm lint && pnpm test   # frontend
```

Both suites must be green. New behaviour needs a test; a fix needs a
regression test that fails on the un-fixed code.

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
