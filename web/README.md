# lm-chat web

React 19 + Vite 8 (Rolldown) + TypeScript 6 + Tailwind 4 SPA for lm-chat v1.

Uses `@vitejs/plugin-react` v6 (Oxc-based) on Vite 8.

## Quick start

```sh
# Install dependencies (requires pnpm 11+)
pnpm install

# Start dev server (proxies /api → localhost:8000)
pnpm dev

# Production build → web/dist/
pnpm build

# Regenerate TypeScript types from an OpenAPI spec
# (the spec itself is a generated artifact, not shipped in the repo —
# run `make emit-openapi` from the project root first to produce
# docs/api/openapi.yaml from the running app, then run this)
pnpm codegen

# Type-check
pnpm typecheck

# Lint
pnpm lint
```

## Tests

```sh
# Unit tests (Vitest + jsdom)
pnpm test:unit

# Unit tests with coverage report
pnpm test:unit --coverage

# E2E tests, route-stubbed (Playwright — starts the Vite preview automatically)
pnpm test:e2e:stubbed

# E2E against a running server (skip the auto webServer)
PLAYWRIGHT_BASE_URL=http://localhost:5173 pnpm test:e2e:stubbed

# E2E against a live FastAPI backend (spawned per-worker by the fixture)
pnpm test:e2e:live
```

The route-stubbed e2e suite stubs `/api/auth/login` so it does not require
a running FastAPI backend. To run that login flow against a real backend,
remove the route stubs in `tests/e2e-stubbed/login.spec.ts` and start the
backend:

```sh
cd .. && uv run uvicorn lmchat.app:app --port 8000
```

## Stack

| Package | Version | Notes |
|---|---|---|
| Vite | ^8.0 | Rolldown-based |
| @vitejs/plugin-react | ^6.0 | Oxc-based |
| @tailwindcss/vite | ^4.3 | CSS-first Tailwind; no tailwind.config.js |
| React | ^19.0 | |
| TypeScript | ^6.0 | Full strict mode per ADR-018 |
| Zustand | ^5.0 | Auth + UI state |
| Vitest | ^4.0 | Unit tests |
| Playwright | ^1.61.1 | E2E tests |

## Theme

Tokens live in `src/globals.css` under `@theme`. Dark surfaces are OKLCH
hue-240 blue-tinted. Fonts are self-hosted as woff2 in `public/fonts/`:
Hanken Grotesk (sans), Hubot Sans (display), and Commit Mono (mono).
