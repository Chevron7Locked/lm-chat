# syntax=docker/dockerfile:1.7

# ============================================================================
# lm-chat v1 production image — Chainguard / Wolfi hardened base
#
# Why Wolfi (cgr.dev/chainguard/*):
# - Built on Wolfi (glibc-based, container-native rolling release)
# - Daily rebuilds — CVE fixes land within hours of upstream patches
# - Near-zero known CVEs at any given moment (typically 0-3 LOW informational)
# - Tracks current Python (3.14.x — matches pyproject)
# - Runs as non-root (uid 65532)
# - Signed SBOMs + cosign attestations feed cert primitive #7 directly
#
# The build stages use the distroless :latest / :latest-dev Python images. The
# RUNTIME stage uses cgr.dev/chainguard/wolfi-base (still Wolfi) rather than the
# distroless python image, so a general Node + uv toolchain can live in the
# container. See the Stage 3 header for the full rationale + tradeoff.
# ============================================================================

# ── Stage 1: frontend build ──────────────────────────────────────────────────
FROM node:22-slim AS frontend

WORKDIR /build/web

# CI=true tells pnpm it's running non-interactively. Without it, pnpm's
# deps-status-check on `pnpm build` aborts with ERR_PNPM_ABORTED_REMOVE_
# MODULES_DIR_NO_TTY when node_modules drift triggers a refresh attempt.
# Caught by P12g 2026-05-21.
ENV CI=true

# Install pnpm
RUN corepack enable && corepack prepare pnpm@latest --activate

# Copy lockfile and manifests first for layer cache.
# pnpm-workspace.yaml carries the `allowBuilds: esbuild: true` directive that
# authorises esbuild's postinstall binary-fetch under pnpm 11's strict-builds
# policy. Without it, `pnpm install --frozen-lockfile` exits non-zero with
# ERR_PNPM_IGNORED_BUILDS for esbuild@0.27.x + 0.28.x (caught by P12g).
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./

RUN pnpm install --frozen-lockfile

# Copy source and build.
# The in-app docs reader (web/src/pages/Docs.tsx) bundles the repo-root
# `guide/` markdown at build time via `import.meta.glob("../../../guide/*.md")`,
# which resolves to /build/guide. Without this COPY the glob finds nothing and
# /docs renders empty (the deployed 404). Must precede `pnpm build`.
COPY guide/ /build/guide/
COPY web/ ./
RUN pnpm build

# ── Stage 2: Python deps ─────────────────────────────────────────────────────
# Chainguard's dev image ships uv + pip + gcc + libffi-dev equivalents
# pre-installed; we can build C-extension wheels (asyncpg, cryptography,
# bcrypt) without any apt-get install layer.
FROM cgr.dev/chainguard/python:latest-dev AS backend-deps

# build-only stage — needs root to install deps; the final runtime stage ends USER nonroot.
# hadolint ignore=DL3002
USER root
WORKDIR /build

COPY pyproject.toml uv.lock ./

# Export the locked deps (workspace project itself excluded — we don't
# need our own package built in this stage; src/ is COPY'd to the runtime
# stage directly) and install into the system site-packages so the
# runtime stage can pick them up via a direct COPY.
RUN uv export --frozen --no-dev --extra postgres --no-hashes \
      --no-emit-workspace \
      -o /tmp/requirements.txt && \
    uv pip install --python python3.14 --system --no-cache \
      --extra-index-url https://pypi.org/simple \
      -r /tmp/requirements.txt

# ── Stage 3: runtime — Wolfi base + Python / Node / uv MCP runtime ────────────
# Switched from the distroless cgr.dev/chainguard/python:latest to
# cgr.dev/chainguard/wolfi-base so the MCP Store's stdio servers — spawned as
# `npx -y <pkg>` (npm servers) or `uvx <pkg>` (python servers) — can run INSIDE
# the container. Rationale:
#
# - The MCP Store lets the operator install ANY MCP server. Almost all ship as
#   npm packages launched via npx; python-based ones launch via uvx. Both need
#   a general runtime in the app container. A SINGLE Node+uv toolchain runs any
#   of them with NO per-server packaging — a curated-companion-per-server model
#   would defeat an open Store.
# - We stay on Wolfi (same glibc, same daily CVE rebuilds, signed SBOMs) so the
#   security lineage is preserved; apk resolves Node's shared-lib deps cleanly,
#   which hand-copying the node binary into the distroless image does not.
# - TRADEOFF (accepted): wolfi-base ships a shell + apk, so this is no longer
#   "distroless". Running arbitrary MCP packages in-container is itself an
#   arbitrary-code surface, so a minimal-distroless story was already moot for
#   the Store use case. The image still runs as non-root (uid 65532).
# - npx/uvx fetch the server package from the registry on FIRST launch, so the
#   container needs outbound network + a writable cache (configured below).
#   Air-gapped deploys can't fetch new servers — inherent to how these ship.
# Chainguard publishes a single rolling, continuously-patched ':latest' tag (no semver); a digest pin would defeat the auto-patching.
# hadolint ignore=DL3007
FROM cgr.dev/chainguard/wolfi-base:latest AS runtime

# Runtimes: Python 3.14 (the app) + Node/npm (npx-based MCP servers) + uv
# (uvx-based MCP servers). wolfi-base already ships the `nonroot` user/group at
# uid/gid 65532, so we reuse it rather than creating one. Wolfi is a rolling
# release; pinning apk versions would fight the daily CVE-patch cadence that is
# the reason we're on Wolfi in the first place.
# hadolint ignore=DL3018
RUN apk add --no-cache python-3.14 nodejs npm uv \
    && ln -sf /usr/bin/python3.14 /usr/bin/python \
    && mkdir -p /data \
                /home/nonroot/logs \
                /home/nonroot/.npm \
                /home/nonroot/.cache/uv \
                /home/nonroot/.local/share \
    && chown -R 65532:65532 /data /home/nonroot

# Third-party site-packages built against Wolfi Python 3.14 ABI. Wolfi's
# python-3.14 keeps site-packages at the standard /usr/lib/python3.14/... so the
# runtime picks them up via Python's default sys.path without PYTHONPATH help.
COPY --from=backend-deps /usr/lib/python3.14/site-packages \
     /usr/lib/python3.14/site-packages

# Application source
COPY src/lmchat /home/nonroot/src/lmchat

# User guide — system-prompt guide injection reads these live at runtime
# (lmchat.services.system_guide._resolve_guide_dir walks up from src/lmchat
# to find guide/ next to it, matching this COPY target).
COPY guide /home/nonroot/guide

# Alembic migrations — P11d schema-fingerprint check does
# `importlib.import_module("migrations.versions.0001_baseline")` on startup.
# Without this COPY the lifespan crashes with ModuleNotFoundError. (P12g)
COPY migrations /home/nonroot/migrations

# Alembic config — startup migration requires alembic.ini at CWD (/home/nonroot).
COPY alembic.ini /home/nonroot/alembic.ini

# Frontend dist
COPY --from=frontend /build/web/dist /home/nonroot/web/dist

WORKDIR /home/nonroot

# PYTHONPATH carries the *application* paths: /home/nonroot/src for
# `from lmchat...` imports, /home/nonroot for `from migrations.versions...`
# used by the schema-fingerprint check. Third-party deps are already
# discoverable via the default site-packages location above.
ENV PYTHONPATH="/home/nonroot/src:/home/nonroot"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# PEP 540 UTF-8 mode — defensive in case any imported lib reads files
# with encoding="locale".
ENV PYTHONUTF8=1

# MCP runtime: HOME + writable caches for npx (`-y` package fetch) and uvx
# (ephemeral tool environments). The container filesystem is otherwise only
# writable at /data and /home/nonroot, both owned by uid 65532.
ENV HOME=/home/nonroot
ENV NPM_CONFIG_CACHE=/home/nonroot/.npm
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
ENV UV_CACHE_DIR=/home/nonroot/.cache/uv
# The uv cache is a mounted named volume; uv builds sdists in /tmp (overlay fs).
# A build that hardlinks the result into the cache then fails with EXDEV
# ("Invalid cross-device link") because /tmp and the volume are different
# devices — seen live on paper-search-mcp's sgmllib3k build. Copy instead of
# hardlink across that boundary.
ENV UV_LINK_MODE=copy
ENV XDG_CACHE_HOME=/home/nonroot/.cache
ENV XDG_DATA_HOME=/home/nonroot/.local/share

EXPOSE 8000

USER nonroot

# ENTRYPOINT is python so the main CMD's args are appended directly (do NOT
# prefix CMD with "python" — that becomes a positional script argument).
# HEALTHCHECK is the exception: Docker execs its CMD directly and does NOT
# prepend ENTRYPOINT, so the healthcheck below must name "python" explicitly
# (otherwise it tries to exec a binary literally named "-c" and every probe
# fails with `exec: "-c": not found`, flapping the container to unhealthy).
ENTRYPOINT ["python"]

# Deliberately probes /healthz (always 200 — process-liveness only), NOT
# /readyz. This HEALTHCHECK drives container restart/rotation, and
# readiness (DB + session-store reachability) is a Service-routing
# concern, not a process-liveness one — a transient DB hiccup shouldn't
# restart an otherwise-healthy process. (/readyz no longer gates on LM
# Studio reachability either way: an admin closing the LM Studio desktop
# app is normal, expected operation for this local-first app and must not
# affect readiness — see src/lmchat/routes/_meta.py.) If you need a
# readiness signal for a load balancer or orchestrator, gate it on /readyz
# there (not here) and never point restart/rotation logic at it — see
# deploy/k8s/deployment.yaml and deploy/k8s/README.md.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", \
         "import urllib.request, sys; r = urllib.request.urlopen('http://localhost:8000/healthz', timeout=2); sys.exit(0 if r.status == 200 else 1)"]

CMD ["-m", "uvicorn", "lmchat.app:app", "--host", "0.0.0.0", "--port", "8000"]
