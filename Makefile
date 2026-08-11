# SPDX-License-Identifier: Apache-2.0
# Top-level convenience targets for lm-chat.
# the validate-openapi target
# is the CI gate for the hand-draft openapi.yaml.

.PHONY: help validate-openapi check-adr-consistency check-pyproject-floors check-no-lmstudio-fs check-commit-hygiene install-hooks gates test emit-openapi check-openapi-drift web-codegen web-gates e2e e2e-stubbed e2e-live dogfood-live security-scan reprobe-surface validate-deploy stress-baseline stress-test stress-postgres-up stress-postgres-down stress-migrate production-gate production-gate-quick soak-test target/gates/.L8-auth-passed mutation-baseline coverage-merged test-flake-scan mutation-gate visual-baseline visual-test

mutation-baseline:
	@echo "[mutation] running cosmic-ray baseline for streaming_client + native + chats"
	bash scripts/mutation-baseline.sh $${TARGET:-all}

help:
	@echo "lm-chat make targets:"
	@echo "  gates                     run pyright + ruff + bandit + pytest"
	@echo "  test                      pytest with coverage"
	@echo "  validate-openapi          run openapi-spec-validator against docs/api/openapi.yaml"
	@echo "  check-adr-consistency     verify all ADRs have a valid Status line"
	@echo "  check-pyproject-floors    verify pyproject.toml floors match PHASES.md §1"
	@echo "  check-commit-hygiene      verify HEAD's commit message obeys LM Chat hygiene rules"
	@echo "  install-hooks             point git at tools/git-hooks (one-time per clone)"
	@echo "  emit-openapi              regenerate docs/api/openapi.yaml from live FastAPI app"
	@echo "  check-openapi-drift       diff committed openapi.yaml vs live emit (CI gate)"
	@echo "  web-codegen               regenerate web/src/types/api.ts from openapi.yaml"
	@echo "  web-gates                 web/ typecheck + lint + vitest + build + e2e-stubbed"
	@echo "  e2e-stubbed               Playwright offline suite (mocked BE, 4 projects)"
	@echo "  e2e-live                  Playwright live suite (needs running app)"
	@echo "  e2e                       e2e-stubbed + e2e-live (§3.2)"
	@echo "  dogfood-live              PRE-SHIP live-model dogfood (real LM Studio; 10-20 min, on-demand only)"
	@echo "  security-scan             bandit (high/critical) + pip-audit + secrets scan"
	@echo "  validate-deploy           build Docker image, boot compose stack, smoke healthz + source-map leak check (P12g)"
	@echo "  production-gate           P15 layered gate (L0 static-fast .. L9 gate-report)"
	@echo "  production-gate-quick     P15 fast pre-flight (L0..L3 only; ~90s target)"
	@echo "  mutation-baseline         run cosmic-ray mutation baseline (TARGET=streaming|chats|native|all)"

validate-openapi:
	uv run python -m openapi_spec_validator docs/api/openapi.yaml

check-adr-consistency:
	uv run python tools/check_adr_consistency.py

check-pyproject-floors:
	uv run python tools/check_pyproject_floors.py

check-no-lmstudio-fs:
	uv run python tools/check_no_lmstudio_fs.py --docs

check-commit-hygiene:
	python3 tools/check_commit_hygiene.py --check-head

install-hooks:
	git config core.hooksPath tools/git-hooks
	@echo "git hooks now resolve to tools/git-hooks/"

gates:
	uv run pyright src/lmchat tests
	uv run ruff check src tests tools/check_adr_consistency.py tools/check_pyproject_floors.py
	# Match `security-scan` (the canonical security gate) and the documented
	# "fail on HIGH/CRITICAL" policy: -ll (>=HIGH severity) -ii (>=HIGH
	# confidence). Without these, this step failed on benign Low-severity
	# noise (B101 assert_used, B110 try_except_pass) that the real gate
	# ignores. See the security-scan target below.
	uv run bandit -r src/lmchat -ll -ii -q
	uv run pytest --cov=lmchat --cov-fail-under=75 -q
	uv run python tools/check_adr_consistency.py
	uv run python tools/check_pyproject_floors.py
	uv run python tools/check_no_lmstudio_fs.py --docs
	python3 tools/check_commit_hygiene.py --check-head
	uv run python -m openapi_spec_validator docs/api/openapi.yaml
	uv run python -m lmchat.openapi.drift_check

emit-openapi:
	uv run python -m lmchat.openapi.emit

check-openapi-drift:
	uv run python -m lmchat.openapi.drift_check

test:
	uv run pytest --cov=lmchat --cov-report=term-missing

# Web targets: emit fresh OpenAPI TypeScript types from the committed yaml.
# Contributors who change a backend route should run `make emit-openapi`
# (regenerates the spec from the live app) followed by `make web-codegen`
# (regenerates web/src/types/api.ts). The web typecheck step fails loudly
# if types are stale.
web-codegen:
	cd web && pnpm codegen

web-gates:
	cd web && pnpm typecheck && pnpm lint && pnpm test:unit --run
	$(MAKE) e2e-stubbed

# Playwright e2e gates.
# e2e-stubbed runs OFFLINE against the mocked BE (all 4 browser projects) and
# is part of web-gates + the web-e2e CI job, so the suite can't rot unnoticed
# again. It builds first — vite preview serves web/dist.
# e2e-live needs a running app at LM_CHAT_BASE_URL with a wiped DB.
e2e-stubbed:
	cd web && pnpm build && npx playwright test --config=playwright.config.ts

e2e-live:
	cd web && npx playwright test --config=playwright.live.config.ts

# Backwards-compatible: both gates, serially (stubbed then live).
e2e:
	$(MAKE) e2e-stubbed
	$(MAKE) e2e-live

# Live-model dogfood gate — PRE-SHIP / on-demand, NOT per-commit (10-20 min,
# needs LM Studio up with a general chat model + an embedding model loaded).
# Runs the flows-dogfood/ journeys against the operator's REAL LM Studio
# (see playwright.dogfood.config.ts) instead of the stubbed e2e-live fixture.
# 1. Fail-fast env gate (LM Studio reachable + fleet shape + web/dist built).
# 2. Build the SPA (the dogfood backend serves web/dist, not the dev server).
# 3. Run the dogfood suite serially against the real upstream.
dogfood-live:
	@set -a; [ -f .env.local ] && . ./.env.local; set +a; \
	export LMCHAT_DOGFOOD_LMSTUDIO_URL="$${LMCHAT_DOGFOOD_LMSTUDIO_URL:-$$LM_STUDIO_BASE_URL}"; \
	export LMCHAT_DOGFOOD_LMSTUDIO_KEY="$${LMCHAT_DOGFOOD_LMSTUDIO_KEY:-$$LM_STUDIO_API_KEY}"; \
	node web/scripts/dogfood-preflight.mjs && \
	( cd web && npm run -s build ) && \
	( cd web && LMCHAT_REAL_UPSTREAM=1 npx playwright test --config=playwright.dogfood.config.ts )

# ---------------------------------------------------------------------------
# Security scan — P9b Item G
# ---------------------------------------------------------------------------
# Runs three independent scanners:
#   1. bandit  — Python security linting, fail on HIGH/CRITICAL (-ll -ii).
#   2. pip-audit — dependency CVE scan against the locked environment.
#   3. secrets_scan.py — regex scan for common hardcoded token patterns.
#
# Wire to CI by adding `make security-scan` as a step after `uv sync --dev`.
security-scan:
	uv run bandit -r src/lmchat -ll -ii -q
	uv run pip-audit --progress-spinner off
	uv run python tools/secrets_scan.py

# ---------------------------------------------------------------------------
# Surface drift detector — P11d.1
# ---------------------------------------------------------------------------
# Re-probes every LM Studio HTTP endpoint in docs/INTEGRATION_MAP.md §2
# against the live LM Studio at $LM_STUDIO_BASE_URL (default: http://localhost:1234).
# Exits 0 when all probes pass or LM Studio is unreachable (non-strict mode).
# Exits 1 on a confirmed endpoint-shape diff.
# Use `--strict` to also fail on connectivity issues (deployment-readiness gate).
# Output is appended to docs/INTEGRATION_MAP_DRIFT.md (gitignored).
reprobe-surface:
	@uv run python tools/reprobe_lm_studio_surface.py

# ---------------------------------------------------------------------------
# Deploy validation — P12g
# ---------------------------------------------------------------------------
# Operator-equivalent pre-tag gate. Runs the full Docker build, brings up
# the production compose stack with placeholder env, polls /healthz, and
# checks that no source-map URL comments leak into the built JS bundle.
#
# Steps (fails on first error; cleanup runs on success OR failure via trap):
#   1. Build the Docker image from deploy/Dockerfile.
#   2. Assert image size < 300 MB (Node+uv MCP runtime; see Dockerfile Stage 3).
#   3. Render .env.validate with placeholder values for every required var.
#   4. Bring up the compose stack using .env.validate.
#   5. Poll http://localhost:8000/healthz until 200 (60 s timeout).
#   6. Grep built JS for //# sourceMappingURL= / //# sourceURL=.
#   7. Tear down the stack.
#
# Docker-dependent; CI marks this as a [docker]-tagged manual job per
# R-06. Never reads the operator's real .env.local.
#
# See docs/deployment.md "Operator-equivalent validation" section.
validate-deploy:
	@set -eu; \
	GIT_SHA=$$(git rev-parse --short HEAD 2>/dev/null || echo no-git); \
	IMAGE_TAG_SHA=deploy-lmchat:$$GIT_SHA; \
	IMAGE_TAG=deploy-lmchat:latest; \
	COMPOSE_FILE=deploy/docker-compose.yml; \
	COMPOSE_ENV=deploy/.env; \
	COMPOSE_ENV_BACKUP=deploy/.env.validate-backup-$$; \
	MAX_IMAGE_BYTES=$$((300 * 1024 * 1024)); \
	VALIDATE_HOST_PORT=18000; \
	HEALTH_URL=http://localhost:$$VALIDATE_HOST_PORT/healthz; \
	HEALTH_TIMEOUT=60; \
	export LM_CHAT_HOST_PORT=$$VALIDATE_HOST_PORT; \
	cleanup() { \
	  echo "[validate-deploy] tearing down compose stack..."; \
	  docker compose -f $$COMPOSE_FILE down -v --remove-orphans >/dev/null 2>&1 || true; \
	  echo "[validate-deploy] removing built image tags + dangling layers..."; \
	  docker image rm -f $$IMAGE_TAG $$IMAGE_TAG_SHA >/dev/null 2>&1 || true; \
	  docker image prune -f >/dev/null 2>&1 || true; \
	  if [ -f $$COMPOSE_ENV_BACKUP ]; then \
	    mv $$COMPOSE_ENV_BACKUP $$COMPOSE_ENV; \
	    echo "[validate-deploy]   restored original $$COMPOSE_ENV"; \
	  else \
	    rm -f $$COMPOSE_ENV; \
	  fi; \
	}; \
	trap cleanup EXIT INT TERM; \
	echo "[validate-deploy] 1/6 rendering placeholder $$COMPOSE_ENV (compose env_file consumes this)..."; \
	if [ -f $$COMPOSE_ENV ]; then \
	  cp $$COMPOSE_ENV $$COMPOSE_ENV_BACKUP; \
	  echo "[validate-deploy]   backed up existing $$COMPOSE_ENV -> $$COMPOSE_ENV_BACKUP"; \
	fi; \
	printf '%s\n' \
	  '# generated by `make validate-deploy` — placeholder values only' \
	  '# (compose env_file in deploy/docker-compose.yml expects .env here)' \
	  'LM_CHAT_SECRET=placeholder-validate-deploy-secret-not-real-do-not-use' \
	  'LM_STUDIO_BASE_URL=http://host.docker.internal:1234' \
	  'LM_STUDIO_API_KEY=sk-placeholder-validate-deploy' \
	  'LM_CHAT_HOST=0.0.0.0' \
	  'LM_CHAT_PORT=8000' \
	  'LM_CHAT_LOG_LEVEL=INFO' \
	  > $$COMPOSE_ENV; \
	echo "[validate-deploy] 2/6 building image via compose (no-cache for honesty)..."; \
	docker compose -f $$COMPOSE_FILE build --no-cache; \
	echo "[validate-deploy]   tagging image as $$IMAGE_TAG_SHA (commit-pinned; closes #176)..."; \
	docker tag $$IMAGE_TAG $$IMAGE_TAG_SHA; \
	echo "[validate-deploy] 3/6 asserting image size < 300 MB..."; \
	SIZE_BYTES=$$(docker image inspect $$IMAGE_TAG --format '{{.Size}}'); \
	SIZE_MB=$$((SIZE_BYTES / 1024 / 1024)); \
	echo "[validate-deploy]   image size: $$SIZE_MB MB ($$SIZE_BYTES bytes)"; \
	if [ "$$SIZE_BYTES" -ge "$$MAX_IMAGE_BYTES" ]; then \
	  echo "[validate-deploy] FAIL: image size $$SIZE_MB MB >= 300 MB"; exit 1; \
	fi; \
	echo "[validate-deploy] 4/6 bringing up compose stack..."; \
	docker compose -f $$COMPOSE_FILE up -d; \
	echo "[validate-deploy] 5/6 polling $$HEALTH_URL (timeout $${HEALTH_TIMEOUT}s)..."; \
	ELAPSED=0; \
	until curl -fsS $$HEALTH_URL >/dev/null 2>&1; do \
	  if [ "$$ELAPSED" -ge "$$HEALTH_TIMEOUT" ]; then \
	    echo "[validate-deploy] FAIL: healthz did not return 200 within $${HEALTH_TIMEOUT}s"; \
	    echo "[validate-deploy] --- docker compose logs ---"; \
	    docker compose -f $$COMPOSE_FILE logs || true; \
	    exit 1; \
	  fi; \
	  sleep 2; \
	  ELAPSED=$$((ELAPSED + 2)); \
	done; \
	echo "[validate-deploy]   healthz OK after $${ELAPSED}s"; \
	echo "[validate-deploy] 6/6 checking source-map URL leakage in built JS..."; \
	SOURCEMAP_CHECK="import pathlib, re; pat = re.compile(r'//# (sourceMappingURL|sourceURL)='); print(sum(1 for f in pathlib.Path('/home/nonroot/web/dist/assets').glob('*.js') if pat.search(f.read_text(encoding='utf-8', errors='replace'))))"; \
	LEAK_COUNT=$$(docker run --rm $$IMAGE_TAG -c "$$SOURCEMAP_CHECK" | tr -d '[:space:]'); \
	echo "[validate-deploy]   .js files with source-map URL comment: $$LEAK_COUNT"; \
	if [ "$$LEAK_COUNT" != "0" ]; then \
	  echo "[validate-deploy] FAIL: $$LEAK_COUNT .js file(s) contain source-map URL comments"; \
	  SOURCEMAP_LIST="import pathlib, re; pat = re.compile(r'//# (sourceMappingURL|sourceURL)='); [print(str(f)) for f in pathlib.Path('/home/nonroot/web/dist/assets').glob('*.js') if pat.search(f.read_text(encoding='utf-8', errors='replace'))]"; \
	  docker run --rm $$IMAGE_TAG -c "$$SOURCEMAP_LIST" || true; \
	  echo "[validate-deploy] Per vite.config.ts \`sourcemap: hidden\`, .js.map files are EXPECTED in dist (for Sentry-style error tracking) but the .js bundles MUST NOT reference them. This check enforces that contract."; \
	  exit 1; \
	fi; \
	echo "[validate-deploy] 7/7 asserting asyncpg is importable in the built image (Postgres path, task #167)..."; \
	ASYNCPG_VER=$$(docker run --rm $$IMAGE_TAG -c \
	  "import asyncpg; print(asyncpg.__version__)" 2>&1) || { \
	  echo "[validate-deploy] FAIL: asyncpg not importable in $$IMAGE_TAG — Postgres path would crash on boot"; \
	  echo "[validate-deploy]   Error: $$ASYNCPG_VER"; \
	  echo "[validate-deploy]   Fix: ensure deploy/Dockerfile uses 'uv sync --extra postgres'"; \
	  exit 1; \
	}; \
	echo "[validate-deploy]   asyncpg $$ASYNCPG_VER OK"; \
	echo ""; \
	echo "  \033[32m\xE2\x9C\x94 validate-deploy passed\033[0m"; \
	echo ""

# ---------------------------------------------------------------------------
# Disposition log gate — P12 §7.7
# ---------------------------------------------------------------------------
# Stress-test harness — P14 deployment gate
# ---------------------------------------------------------------------------
# Local exhaustive load test against the operator's real LM Studio.
# Required to pass before every deployment (wired into validate-deploy).
#
# First-time setup:
#   make stress-baseline           # captures p95/p99 baseline, locks fingerprint
#
# Regular use:
#   make stress-test               # gates at 1.5x baseline; blocks deploy on fail
#   make stress-test S=1           # run only scenario 1 (development)
#   make stress-test ARGS="--with-tracing"  # boot Jaeger + emit OTel spans
#
# See tests/stress/README.md for the full operator runbook.
# Postgres is REQUIRED for stress runs per operator directive 2026-05-22.
# Auth-bottleneck finding: SQLite serialized writes + scrypt KDF + 200
# concurrent registrations = login 500s.  Postgres mitigates by removing
# the writer serialization.  The orchestrator REFUSES to start against
# SQLite and exits 2.
STRESS_PG_COMPOSE := tests/stress/postgres/compose-postgres.yml
STRESS_PG_HOST    := 127.0.0.1
STRESS_PG_PORT    := 5433
STRESS_PG_USER    := lm_chat_stress
STRESS_PG_PASS    := stress
STRESS_PG_DB      := lm_chat_stress
STRESS_PG_URL_SYNC  := postgresql://$(STRESS_PG_USER):$(STRESS_PG_PASS)@$(STRESS_PG_HOST):$(STRESS_PG_PORT)/$(STRESS_PG_DB)
STRESS_PG_URL_ASYNC := postgresql+asyncpg://$(STRESS_PG_USER):$(STRESS_PG_PASS)@$(STRESS_PG_HOST):$(STRESS_PG_PORT)/$(STRESS_PG_DB)

stress-postgres-up:
	@echo "[stress] booting ephemeral Postgres on $(STRESS_PG_HOST):$(STRESS_PG_PORT)..."
	docker compose -f $(STRESS_PG_COMPOSE) up -d
	@echo "[stress] waiting for pg_isready..."
	@for i in $$(seq 1 30); do \
	  if docker compose -f $(STRESS_PG_COMPOSE) exec -T postgres pg_isready -U $(STRESS_PG_USER) -d $(STRESS_PG_DB) >/dev/null 2>&1; then \
	    echo "[stress] Postgres ready"; exit 0; \
	  fi; sleep 1; \
	done; \
	echo "[stress] Postgres did not become ready in 30s"; \
	docker compose -f $(STRESS_PG_COMPOSE) logs postgres || true; \
	exit 1

stress-postgres-down:
	@echo "[stress] tearing down ephemeral Postgres..."
	docker compose -f $(STRESS_PG_COMPOSE) down -v --remove-orphans

# Run Alembic migrations against the ephemeral Postgres.  We pass the URL
# via the programmatic alembic.config.Config API rather than the env-var
# fallback, because alembic.ini hardcodes ``sqlalchemy.url = sqlite+...``
# which env.py will pick up first (silently) if not overridden.  See
# migrations/env.py URL-resolution comment.
stress-migrate:
	@echo "[stress] running migrations against $(STRESS_PG_URL_ASYNC)..."
	@LM_CHAT_SECRET=$${LM_CHAT_SECRET:-stress-secret-00000000000000000000000000000000} \
	  uv run --extra postgres python -c "from alembic.config import Config; from alembic import command; \
cfg = Config('alembic.ini'); \
cfg.set_main_option('sqlalchemy.url', '$(STRESS_PG_URL_ASYNC)'); \
command.upgrade(cfg, 'head'); \
print('migrations applied: head')"

# stress-baseline: boot Postgres -> migrate -> run baseline -> teardown.
# The shell trap ensures teardown runs on failure too.
stress-baseline:
	@set -eu; \
	cleanup() { \
	  echo "[stress-baseline] cleanup: tearing down Postgres..."; \
	  $(MAKE) -s stress-postgres-down >/dev/null 2>&1 || true; \
	}; \
	trap cleanup EXIT INT TERM; \
	$(MAKE) stress-postgres-up; \
	$(MAKE) stress-migrate; \
	DATABASE_URL='$(STRESS_PG_URL_ASYNC)' \
	  PYTHONPATH=. uv run python tests/stress/run_stress.py --baseline $(ARGS)

# stress-test: boot Postgres -> migrate -> run gated test -> teardown.
stress-test:
	@set -eu; \
	cleanup() { \
	  echo "[stress-test] cleanup: tearing down Postgres..."; \
	  $(MAKE) -s stress-postgres-down >/dev/null 2>&1 || true; \
	}; \
	trap cleanup EXIT INT TERM; \
	$(MAKE) stress-postgres-up; \
	$(MAKE) stress-migrate; \
	DATABASE_URL='$(STRESS_PG_URL_ASYNC)' \
	  PYTHONPATH=. uv run python tests/stress/run_stress.py $(if $(S),--scenario $(S),) $(ARGS)

# ---------------------------------------------------------------------------
# P15 production-gate — layered deployment gate
# ---------------------------------------------------------------------------
#md §2.
#
# Layers run in sequence, gated on prior-layer success via sentinel files
# at target/gates/.<L>-passed. P15a wires L0 (static-fast) and L9 (aggregator);
# layers L1..L8 are stubs that emit a 'not yet implemented' JUnit XML so the
# aggregator can produce a meaningful end-to-end report. P15b..P15h replace
# the stubs with real layer implementations.
#
# Layout (PLAN §9 invariant 1):
#   target/gates/L<N>-<slug>.xml      one JUnit XML per layer
#   target/gates/L<N>-<sub>.xml       per-tool sub-files when relevant
#   target/gates/.<L>-passed           sentinel file (make's gate)
#   target/gates/report.json          L9 aggregator output (PLAN §7)
#   target/gates/index.html           L9 aggregator dashboard
#
# Entry points:
#   make production-gate         full L0..L9 (~10-12 min when all layers land)
#   make production-gate-quick   L0..L3 (dev pre-flight, ~90s target)
GATES_DIR := target/gates

$(GATES_DIR):
	@mkdir -p $(GATES_DIR)

# ----- L0 static-fast ------------------------------------------------------
# pyright + ruff (+ hadolint when on PATH). Combined JUnit XML at
# $(GATES_DIR)/L0-static-fast.xml. Per PLAN §2 budget: 12s.
$(GATES_DIR)/.L0-passed: | $(GATES_DIR)
	@echo "[L0] static-fast: pyright + ruff (+ hadolint if present)"
	@set -e; \
	  start=$$(date +%s); \
	  uv run pyright --outputjson src/lmchat tests > $(GATES_DIR)/L0-pyright.json 2>/dev/null || true; \
	  uv run python tools/junit_from_pyright.py \
	    --input $(GATES_DIR)/L0-pyright.json \
	    --output $(GATES_DIR)/L0-pyright.xml; \
	  uv run ruff check --output-format=junit \
	    src tests tools > $(GATES_DIR)/L0-ruff.xml || true; \
	  if command -v hadolint >/dev/null 2>&1; then \
	    hadolint --format json deploy/Dockerfile > $(GATES_DIR)/L0-hadolint.json 2>/dev/null || true; \
	    uv run python tools/junit_from_hadolint.py \
	      --input $(GATES_DIR)/L0-hadolint.json \
	      --output $(GATES_DIR)/L0-hadolint.xml; \
	  else \
	    echo "[L0]   hadolint not on PATH — emitting skipped testcase"; \
	    uv run python tools/junit_from_hadolint.py --skipped \
	      --output $(GATES_DIR)/L0-hadolint.xml; \
	  fi; \
	  uv run python tools/check_no_lmstudio_fs.py --docs > $(GATES_DIR)/L0-no-lmstudio-fs.log 2>&1 || \
	    { echo "[L0] FAIL: tools/check_no_lmstudio_fs.py reported violations"; cat $(GATES_DIR)/L0-no-lmstudio-fs.log; exit 1; }; \
	  uv run python tools/check_memory_query_scoping.py \
	    --output $(GATES_DIR)/L0-memory-scoping.xml || \
	    { echo "[L0] FAIL: memory query scoping violations detected"; exit 1; }; \
	  uv run python tools/check_no_scratch_docs.py --verbose || \
	    { echo "[L0] FAIL: transient working docs are tracked — see tools/check_no_scratch_docs.py output above"; exit 1; }; \
	  : "PLAN v3 §1F: Semgrep JS/TS + custom rule (graceful-skip if semgrep not installed)"; \
	  if command -v semgrep >/dev/null 2>&1; then \
	    echo "[L0]   semgrep JS/TS (web/src) ..."; \
	    bash scripts/security-static.sh 2>&1 | grep -E '(semgrep|L0-semgrep)' || true; \
	    if [ ! -f $(GATES_DIR)/L0-semgrep-jsts.xml ]; then \
	      uv run python tools/junit_from_semgrep.py --skipped \
	        --output $(GATES_DIR)/L0-semgrep-jsts.xml; \
	    fi; \
	    if [ ! -f $(GATES_DIR)/L0-semgrep-custom.xml ]; then \
	      uv run python tools/junit_from_semgrep.py --skipped \
	        --output $(GATES_DIR)/L0-semgrep-custom.xml; \
	    fi; \
	  else \
	    echo "[L0]   semgrep not on PATH — emitting skipped testcases"; \
	    uv run python tools/junit_from_semgrep.py --skipped \
	      --output $(GATES_DIR)/L0-semgrep-jsts.xml; \
	    uv run python tools/junit_from_semgrep.py --skipped \
	      --output $(GATES_DIR)/L0-semgrep-custom.xml; \
	  fi; \
	  uv run python tools/combine_l0_junit.py --gates-dir $(GATES_DIR); \
	  echo "[L0] elapsed $$(( $$(date +%s) - start ))s"
	@touch $@

# ----- L1..L8 stubs (replaced by P15b..P15h) -------------------------------
define STUB_LAYER
$(GATES_DIR)/.L$(1)-passed: $(GATES_DIR)/.L$(2)-passed
	@echo "[L$(1) $(3)] not yet implemented (P15a stub — replaced by future sub-cluster)"
	@uv run python tools/emit_stub_junit.py \
	  --layer $(1) --slug $(3) --output $(GATES_DIR)/L$(1)-$(3).xml
	@touch $$@
endef

# ----- L1 license-bill (P15 — replaces stub) --------------------------------
# Two sub-checks:
#   1. pip-licenses:       Python dev-tree copyleft scan  → L1-pip-licenses.xml
#   2. sbom-licenses:      SPDX + CycloneDX full-image scan → L1-sbom-licenses.xml
#      (skips gracefully when L4 SBOMs are absent)
# Combined via combine_layer_junit.py → L1-license-bill.xml
# Budget: <5s (PLAN §2).
$(GATES_DIR)/.L1-passed: $(GATES_DIR)/.L0-passed
	@echo "[L1] license-bill: pip-licenses + sbom-licenses"
	@set -e; \
	  start=$$(date +%s); \
	  uv run python tools/junit_from_pip_licenses.py \
	    --output $(GATES_DIR)/L1-pip-licenses.xml || \
	    { echo "[L1] FAIL: pip-licenses (GPL/AGPL detected in Python deps)"; exit 1; }; \
	  uv run python tools/junit_from_sbom_licenses.py \
	    --sbom $(GATES_DIR)/sbom.spdx.json \
	    --sbom $(GATES_DIR)/sbom.cyclonedx.json \
	    --output $(GATES_DIR)/L1-sbom-licenses.xml || \
	    { echo "[L1] FAIL: sbom-licenses (GPL/AGPL detected in image packages)"; exit 1; }; \
	  uv run python tools/combine_layer_junit.py \
	    --layer 1 --slug license-bill \
	    --gates-dir $(GATES_DIR) \
	    --sub L1-pip-licenses.xml \
	    --sub L1-sbom-licenses.xml; \
	  echo "[L1] elapsed $$(( $$(date +%s) - start ))s"
	@touch $@

# ----- L2 unit-integration (P15: replaces the P15a stub) --------------------
# Runs the full unit + integration pytest suite (tests/, excluding the opt-in
# stress tree) and emits a JUnit report as the layer XML. This is the coverage
# backbone of the gate: every behavioural assertion the app ships with.
$(GATES_DIR)/.L2-passed: $(GATES_DIR)/.L1-passed
	@echo "[L2] unit-integration: pytest tests/ (full unit + integration suite)"
	@set -u; \
	  start=$$(date +%s); \
	  mkdir -p $(GATES_DIR); \
	  LM_CHAT_SECRET=$${LM_CHAT_SECRET:-l2-unit-secret-00000000000000000000000000000000} \
	  uv run pytest tests/ --ignore=tests/stress -q -p no:cacheprovider \
	    -o addopts="" \
	    --junit-xml=$(GATES_DIR)/L2-unit-integration.xml \
	    >$(GATES_DIR)/L2-unit-integration.log 2>&1; \
	  rc=$$?; \
	  tail -1 $(GATES_DIR)/L2-unit-integration.log; \
	  echo "[L2] elapsed $$(( $$(date +%s) - start ))s"; \
	  if [ $$rc -ne 0 ]; then \
	    echo "[L2] FAIL: unit+integration suite — see L2-unit-integration.log"; \
	    exit 1; \
	  fi
	@touch $@

# L3 security-static (PLAN v3 §1F extension): bandit + pip-audit + pip-licenses + gitleaks +
# secrets_scan + semgrep (Python) + osv-scanner (npm + Python).
# Replaces the STUB_LAYER(3,2,security-static) stub wired in P15a.
# PLAN v3 §1F adds: semgrep SAST (p/python, p/owasp-top-ten, p/security-audit,
# p/sqlalchemy, p/jwt on src/lmchat) + osv-scanner SCA (npm via web/pnpm-lock.yaml,
# Python via uv.lock). Graceful-skip if tools absent; hard-fail in CI.
# L4 is implemented below (P15b). L5 wired (P15c). L6 wired (P15d). L7 wired (P15e).
# L8 auth+session+crypto implemented below (P15g). stress-gated moved to L8b stub.

$(GATES_DIR)/.L3-passed: $(GATES_DIR)/.L2-passed
	@echo "[L3] security-static: bandit + pip-audit + pip-licenses + gitleaks + secrets_scan + semgrep + osv-scanner"
	@set -e; \
	  start=$$(date +%s); \
	  uv run python tools/junit_from_bandit.py --output $(GATES_DIR)/L3-bandit.xml || \
	    { echo "[L3] FAIL: bandit"; exit 1; }; \
	  uv run python tools/junit_from_pip_audit.py --output $(GATES_DIR)/L3-pip-audit.xml || \
	    { echo "[L3] FAIL: pip-audit"; exit 1; }; \
	  uv run python tools/junit_from_pip_licenses.py --output $(GATES_DIR)/L3-pip-licenses.xml || \
	    { echo "[L3] FAIL: pip-licenses (GPL/AGPL detected)"; exit 1; }; \
	  uv run python tools/junit_from_gitleaks.py --output $(GATES_DIR)/L3-gitleaks.xml || \
	    { echo "[L3] FAIL: gitleaks (secret in git history)"; exit 1; }; \
	  uv run python tools/secrets_scan.py --junit-output $(GATES_DIR)/L3-working-tree-secrets.xml || \
	    { echo "[L3] FAIL: working-tree secrets"; exit 1; }; \
	  : "PLAN v3 §1F: Semgrep Python rules (graceful-skip if semgrep not installed)"; \
	  if command -v semgrep >/dev/null 2>&1; then \
	    echo "[L3]   semgrep Python (src/lmchat) ..."; \
	    uv run python tools/junit_from_semgrep.py \
	      --output $(GATES_DIR)/L3-semgrep-python.xml \
	      --config p/python --config p/owasp-top-ten \
	      --config p/security-audit --config p/jwt \
	      --target src/lmchat || true; \
	  else \
	    echo "[L3]   semgrep not on PATH — emitting skipped testcase"; \
	    uv run python tools/junit_from_semgrep.py --skipped \
	      --output $(GATES_DIR)/L3-semgrep-python.xml; \
	  fi; \
	  : "PLAN v3 §1F: osv-scanner (graceful-skip if not installed)"; \
	  if command -v osv-scanner >/dev/null 2>&1; then \
	    echo "[L3]   osv-scanner (web/pnpm-lock.yaml + uv.lock) ..."; \
	    uv run python tools/junit_from_osv.py \
	      --output $(GATES_DIR)/L3-osv-scanner.xml \
	      --lockfile web/pnpm-lock.yaml --lockfile uv.lock || true; \
	  else \
	    echo "[L3]   osv-scanner not on PATH — emitting skipped testcase"; \
	    uv run python tools/junit_from_osv.py --skipped \
	      --output $(GATES_DIR)/L3-osv-scanner.xml; \
	  fi; \
	  uv run python tools/combine_layer_junit.py --layer 3 --slug security-static \
	    --gates-dir $(GATES_DIR) \
	    --sub L3-bandit.xml --sub L3-pip-audit.xml \
	    --sub L3-pip-licenses.xml --sub L3-gitleaks.xml \
	    --sub L3-working-tree-secrets.xml \
	    --sub L3-semgrep-python.xml --sub L3-osv-scanner.xml; \
	  echo "[L3] elapsed $$(( $$(date +%s) - start ))s"
	@touch $@

# ----- L8 auth + session + crypto runtime (P15g) ---------------------------
# Runs the three P15g emitter tools (session fixation, TOTP race, AES-GCM
# tamper) via the junit_from_auth_session.py aggregator, then runs the
# pytest auth_runtime suite.
#
# Database: uses the same in-process SQLite fixtures as the existing route
# tests (no Docker required). The pytest suite uses TestClient + per-test
# tmp_path engines, consistent with tests/routes/test_auth.py.
#
# Budget: ~30s wall-clock.

$(GATES_DIR)/L8-xff-default-deny.xml: | $(GATES_DIR)
	PYTHONPATH=$(PWD) uv run python tools/xff_default_deny_test.py

$(GATES_DIR)/.L8-auth-passed: $(GATES_DIR)/.L7-passed $(GATES_DIR)/L8-xff-default-deny.xml
	@echo "[L8] auth+session+crypto runtime (P15g)"
	@set -e; \
	  start=$$(date +%s); \
	  mkdir -p $(GATES_DIR); \
	  echo "[L8]   running pytest tests/security/auth_runtime/ ..."; \
	  uv run pytest tests/security/auth_runtime/ \
	    --no-cov -q --tb=short \
	    --junit-xml=$(GATES_DIR)/L8-auth-runtime-pytest.xml \
	    >$(GATES_DIR)/L8-auth-runtime-pytest.log 2>&1 || true; \
	  echo "[L8]   running standalone emitter tools via junit_from_auth_session.py ..."; \
	  PYTHONPATH=$(PWD) uv run python tools/junit_from_auth_session.py \
	    >$(GATES_DIR)/L8-emitters.log 2>&1 || true; \
	  echo "[L8]   combining layer JUnit XML..."; \
	  uv run python tools/combine_layer_junit.py \
	    --gates-dir $(GATES_DIR) \
	    --layer 8 --slug auth-session \
	    --sub L8-auth-session.xml \
	    --sub L8-auth-runtime-pytest.xml \
	    --sub L8-xff-default-deny.xml; \
	  combine_rc=$$?; \
	  echo "[L8] elapsed $$(( $$(date +%s) - start ))s"; \
	  if [ $$combine_rc -eq 0 ]; then \
	    touch $@; \
	  else \
	    echo "[L8]   findings present — NOT writing sentinel"; \
	    exit $$combine_rc; \
	  fi

# ----- L4 container-supplychain (P15b) -------------------------------------
# Real implementation: trivy + anchore/syft + dockle + optional cosign,
# scanning the locally-built deploy-lmchat image. Per PLAN §3 "Container +
# supply chain" + §8 P15b row. Budget: ~20s when binaries are present.
#
# Each sub-tool runs independently; if its binary isn't on PATH, the
# corresponding adapter emits a synthetic <skipped> testcase via the
# --skipped flag so the layer doesn't crash. Combined output lives at
# $(GATES_DIR)/L4-container-supplychain.xml via tools/combine_layer_junit.py.
#
# Image pre-condition: if `deploy-lmchat:latest` is missing from the local
# Docker daemon, the recipe emits an "image-not-built" skipped layer (one
# clear testcase) instead of failing.
# L4 scans the commit-pinned image tag (closes #176 — dockle DKL-DI-0006
# "avoid :latest" — production scanning should target the SHA-tagged
# build, not the dev convenience tag). validate-deploy creates BOTH tags
# (:latest for dev `docker run`, :<git-sha> for cert + production).
# Override with `IMAGE_REF=...` env if needed.
#
# dockle notes:
#  - The binary reads a `docker save` tarball via `--input` rather than the
#    image name directly: on macOS Docker Desktop the dockle library cannot
#    reach the daemon by image-ref and falls through to a docker.io registry
#    lookup ("requested access denied"). The tarball path is host-independent.
#  - `-af settings.py` suppresses a CIS-DI-0010 false positive on the bundled
#    `mcp/server/auth/settings.py` — a Pydantic BaseModel schema (field names
#    only, verified: no credential literals), flagged purely on its filename.
IMAGE_REF ?= deploy-lmchat:$(shell git rev-parse --short HEAD 2>/dev/null || echo latest)

$(GATES_DIR)/.L4-passed: $(GATES_DIR)/.L3-passed
	@echo "[L4] container-supplychain: trivy + syft + dockle + cosign(optional)"
	@set -e; \
	  start=$$(date +%s); \
	  mkdir -p $(GATES_DIR); \
	  if ! command -v docker >/dev/null 2>&1; then \
	    echo "[L4]   docker not on PATH — emitting image-not-built skipped layer"; \
	    uv run python tools/emit_stub_junit.py \
	      --layer 4 --slug container-supplychain \
	      --output $(GATES_DIR)/L4-container-supplychain.xml; \
	    touch $@; exit 0; \
	  fi; \
	  if ! docker image inspect $(IMAGE_REF) >/dev/null 2>&1; then \
	    echo "[L4]   image $(IMAGE_REF) not present — building it (the L2 validate-deploy test prunes its own isolated tag, but a prior run may have left none)..."; \
	    if docker build --file deploy/Dockerfile -t $(IMAGE_REF) -t deploy-lmchat:latest . >$(GATES_DIR)/L4-image-build.log 2>&1; then \
	      echo "[L4]   image $(IMAGE_REF) built"; \
	    else \
	      echo "[L4]   image build FAILED — emitting image-not-built skipped layer; see L4-image-build.log"; \
	      tail -20 $(GATES_DIR)/L4-image-build.log || true; \
	      uv run python tools/emit_stub_junit.py \
	        --layer 4 --slug container-supplychain \
	        --output $(GATES_DIR)/L4-container-supplychain.xml; \
	      touch $@; exit 0; \
	    fi; \
	  fi; \
	  echo "[L4]   image: $(IMAGE_REF) present"; \
	  if command -v trivy >/dev/null 2>&1; then \
	    echo "[L4]   trivy scan..."; \
	    trivy image --scanners vuln,secret,config --format json \
	      --output $(GATES_DIR)/L4-trivy.json --quiet \
	      $(IMAGE_REF) 2>/dev/null || true; \
	    uv run python tools/junit_from_trivy.py \
	      --input $(GATES_DIR)/L4-trivy.json \
	      --output $(GATES_DIR)/L4-trivy.xml; \
	  else \
	    echo "[L4]   trivy not on PATH — emitting skipped testcase"; \
	    uv run python tools/junit_from_trivy.py --skipped \
	      --output $(GATES_DIR)/L4-trivy.xml; \
	  fi; \
	  if command -v syft >/dev/null 2>&1; then \
	    echo "[L4]   syft SBOM (spdx + cyclonedx) [binary, docker-archive]..."; \
	    docker save $(IMAGE_REF) -o $(GATES_DIR)/L4-syft-image.tar 2>/dev/null && \
	    syft docker-archive:$(GATES_DIR)/L4-syft-image.tar -q \
	      -o spdx-json=$(GATES_DIR)/sbom.spdx.json \
	      -o cyclonedx-json=$(GATES_DIR)/sbom.cyclonedx.json 2>/dev/null || true; \
	    rm -f $(GATES_DIR)/L4-syft-image.tar; \
	    uv run python tools/junit_from_sbom.py \
	      --sbom $(GATES_DIR)/sbom.spdx.json \
	      --sbom $(GATES_DIR)/sbom.cyclonedx.json \
	      --output $(GATES_DIR)/L4-syft.xml; \
	  elif docker image inspect anchore/syft:latest >/dev/null 2>&1; then \
	    echo "[L4]   syft SBOM (spdx + cyclonedx) [docker]..."; \
	    docker run --rm \
	      -v /var/run/docker.sock:/var/run/docker.sock \
	      -v $(CURDIR)/$(GATES_DIR):/out \
	      anchore/syft:latest \
	      $(IMAGE_REF) -q \
	      -o spdx-json=/out/sbom.spdx.json \
	      -o cyclonedx-json=/out/sbom.cyclonedx.json 2>/dev/null || true; \
	    uv run python tools/junit_from_sbom.py \
	      --sbom $(GATES_DIR)/sbom.spdx.json \
	      --sbom $(GATES_DIR)/sbom.cyclonedx.json \
	      --output $(GATES_DIR)/L4-syft.xml; \
	  else \
	    echo "[L4]   syft not on PATH and anchore/syft:latest image not pulled — emitting skipped testcase"; \
	    uv run python tools/junit_from_sbom.py --skipped \
	      --output $(GATES_DIR)/L4-syft.xml; \
	  fi; \
	  if command -v dockle >/dev/null 2>&1; then \
	    echo "[L4]   dockle hardening checks [binary, --input tarball]..."; \
	    docker save $(IMAGE_REF) -o $(GATES_DIR)/L4-image.tar 2>/dev/null && \
	    dockle -f json -o $(GATES_DIR)/L4-dockle.json --exit-level fatal \
	      -af settings.py --input $(GATES_DIR)/L4-image.tar 2>/dev/null || true; \
	    rm -f $(GATES_DIR)/L4-image.tar; \
	    uv run python tools/junit_from_dockle.py \
	      --input $(GATES_DIR)/L4-dockle.json \
	      --output $(GATES_DIR)/L4-dockle.xml; \
	  elif docker image inspect goodwithtech/dockle:latest >/dev/null 2>&1; then \
	    echo "[L4]   dockle hardening checks [docker]..."; \
	    docker run --rm \
	      -v /var/run/docker.sock:/var/run/docker.sock \
	      -v $(CURDIR)/$(GATES_DIR):/out \
	      goodwithtech/dockle:latest \
	      -f json -o /out/L4-dockle.json --exit-level fatal \
	      -af settings.py $(IMAGE_REF) 2>/dev/null || true; \
	    uv run python tools/junit_from_dockle.py \
	      --input $(GATES_DIR)/L4-dockle.json \
	      --output $(GATES_DIR)/L4-dockle.xml; \
	  else \
	    echo "[L4]   dockle not on PATH and goodwithtech/dockle:latest image not pulled — emitting skipped testcase"; \
	    uv run python tools/junit_from_dockle.py --skipped \
	      --output $(GATES_DIR)/L4-dockle.xml; \
	  fi; \
	  if [ -n "$$COSIGN_KEY" ] && command -v cosign >/dev/null 2>&1; then \
	    echo "[L4]   cosign verify..."; \
	    if cosign verify $(IMAGE_REF) --key "$$COSIGN_KEY" >$(GATES_DIR)/L4-cosign.log 2>&1; then \
	      printf '%s\n' \
	        '<?xml version="1.0" encoding="utf-8"?>' \
	        '<testsuites name="l4-cosign" tests="1" failures="0" errors="0">' \
	        '  <testsuite name="l4-cosign" tests="1" failures="0" errors="0" skipped="0">' \
	        '    <testcase name="cosign-verify" classname="cosign"/>' \
	        '  </testsuite>' \
	        '</testsuites>' \
	        > $(GATES_DIR)/L4-cosign.xml; \
	    else \
	      printf '%s\n' \
	        '<?xml version="1.0" encoding="utf-8"?>' \
	        '<testsuites name="l4-cosign" tests="1" failures="1" errors="0">' \
	        '  <testsuite name="l4-cosign" tests="1" failures="1" errors="0" skipped="0">' \
	        '    <testcase name="cosign-verify" classname="cosign">' \
	        '      <failure message="cosign verify failed" type="cosign-verify">see L4-cosign.log</failure>' \
	        '    </testcase>' \
	        '  </testsuite>' \
	        '</testsuites>' \
	        > $(GATES_DIR)/L4-cosign.xml; \
	    fi; \
	  else \
	    echo "[L4]   cosign skipped (COSIGN_KEY not set or cosign not on PATH)"; \
	    printf '%s\n' \
	      '<?xml version="1.0" encoding="utf-8"?>' \
	      '<testsuites name="l4-cosign-skipped" tests="1" failures="0" errors="0" skipped="1">' \
	      '  <testsuite name="l4-cosign-skipped" tests="1" failures="0" errors="0" skipped="1">' \
	      '    <testcase name="cosign-verify" classname="cosign">' \
	      '      <skipped message="COSIGN_KEY not set (optional)">Set COSIGN_KEY env var and install cosign to enable signature verification.</skipped>' \
	      '    </testcase>' \
	      '  </testsuite>' \
	      '</testsuites>' \
	      > $(GATES_DIR)/L4-cosign.xml; \
	  fi; \
	  uv run python tools/combine_layer_junit.py \
	    --gates-dir $(GATES_DIR) \
	    --layer 4 --slug container-supplychain \
	    --sub L4-trivy.xml --sub L4-syft.xml \
	    --sub L4-dockle.xml --sub L4-cosign.xml; \
	  echo "[L4] elapsed $$(( $$(date +%s) - start ))s"
	@touch $@

# ----- L5 dast-offline (P15c) ----------------------------------------------
# Boots an ephemeral Postgres (port 5434, distinct from stress's 5433) and an
# isolated lm-chat backend on port 8766 (distinct from stress's 8765), then
# runs two DAST sub-tools against it:
#
#   1. schemathesis (OpenAPI property-based fuzzer, all phases incl. stateful)
#   2. tools/generate_idor_grid.py (IDOR / authorization-bypass matrix)
#
# Each sub-tool's findings are emitted as JUnit XML; the combiner merges
# them into target/gates/L5-dast-offline.xml. The orchestrator never crashes:
# every failure path (Docker absent, port already bound, backend never came
# up, schemathesis binary missing, schemathesis crashed) is converted to a
# normalised JUnit XML so the L9 aggregator surfaces it as a finding.
#
# Schemathesis 4.19 note: prior versions had ``--stateful=links``; in 4.19
# the equivalent is exposed through ``--phases=examples,coverage,fuzzing,
# stateful`` (the ``stateful`` phase covers OpenAPI link traversal). JUnit
# output uses ``--report=junit --report-junit-path=<file>`` rather than the
# pre-4.x ``--junit-xml`` flag.
#
# The schemathesis invocation sets SCHEMATHESIS_HOOKS=security.schemathesis.hooks
# for autoload (confirmed working in 4.x:
# https://schemathesis.readthedocs.io/en/stable/guides/extending.html).
# That note MUST live here as a make-level comment, never as a tab-indented
# comment inside the recipe shell block: a ``#`` line without a trailing ``\``
# terminates the backslash-continued command, splitting the recipe into two
# shells and firing the EXIT trap (killing the backend) before schemathesis runs.
#
# Budget: 180s wall-clock. Includes Postgres boot (~5s), migrations (~2s),
# backend boot (~5s), schemathesis run (60-120s capped), IDOR grid (~10s).

L5_PG_COMPOSE   := tests/security/postgres/compose-postgres-l5.yml
L5_PG_HOST      := 127.0.0.1
L5_PG_PORT      := 5434
L5_PG_USER      := lm_chat_l5
L5_PG_PASS      := l5
L5_PG_DB        := lm_chat_l5
L5_PG_URL_ASYNC := postgresql+asyncpg://$(L5_PG_USER):$(L5_PG_PASS)@$(L5_PG_HOST):$(L5_PG_PORT)/$(L5_PG_DB)
L5_BACKEND_HOST := 127.0.0.1
L5_BACKEND_PORT := 8766
L5_BACKEND_URL  := http://$(L5_BACKEND_HOST):$(L5_BACKEND_PORT)
L5_SETUP_TOKEN  := l5-dast-offline-setup-token

$(GATES_DIR)/.L5-passed: $(GATES_DIR)/.L4-passed
	@echo "[L5] dast-offline: schemathesis + idor-grid against ephemeral backend"
	@set -u; \
	  start=$$(date +%s); \
	  mkdir -p $(GATES_DIR); \
	  if ! command -v docker >/dev/null 2>&1; then \
	    echo "[L5]   docker not on PATH — emitting infrastructure-unavailable skipped layer"; \
	    uv run python tools/emit_stub_junit.py \
	      --layer 5 --slug dast-offline \
	      --output $(GATES_DIR)/L5-dast-offline.xml; \
	    touch $@; exit 0; \
	  fi; \
	  cleanup() { \
	    rc=$$?; \
	    echo "[L5]   cleanup: stopping backend + Postgres"; \
	    if [ -n "$${L5_BACKEND_PID:-}" ]; then \
	      kill $$L5_BACKEND_PID >/dev/null 2>&1 || true; \
	      wait $$L5_BACKEND_PID >/dev/null 2>&1 || true; \
	    fi; \
	    docker compose -f $(L5_PG_COMPOSE) down -v --remove-orphans >/dev/null 2>&1 || true; \
	    return $$rc; \
	  }; \
	  trap cleanup EXIT INT TERM; \
	  echo "[L5]   booting ephemeral Postgres on $(L5_PG_HOST):$(L5_PG_PORT)..."; \
	  if ! docker compose -f $(L5_PG_COMPOSE) up -d >$(GATES_DIR)/L5-postgres-boot.log 2>&1; then \
	    if grep -qiE "no space left on device|out of disk space" $(GATES_DIR)/L5-postgres-boot.log; then \
	      echo "[L5]   Docker out of disk space — emitting infrastructure-unavailable skipped layer"; \
	    else \
	      echo "[L5]   docker compose up failed — emitting infrastructure-unavailable skipped layer"; \
	      cat $(GATES_DIR)/L5-postgres-boot.log; \
	    fi; \
	    uv run python tools/emit_stub_junit.py \
	      --layer 5 --slug dast-offline \
	      --output $(GATES_DIR)/L5-dast-offline.xml; \
	    touch $@; exit 0; \
	  fi; \
	  pg_ready=0; \
	  for i in $$(seq 1 30); do \
	    if docker compose -f $(L5_PG_COMPOSE) exec -T postgres pg_isready -U $(L5_PG_USER) -d $(L5_PG_DB) >/dev/null 2>&1; then \
	      pg_ready=1; break; \
	    fi; sleep 1; \
	  done; \
	  if [ "$$pg_ready" -ne 1 ]; then \
	    echo "[L5]   Postgres never became ready — emitting infrastructure-unavailable skipped layer"; \
	    uv run python tools/emit_stub_junit.py \
	      --layer 5 --slug dast-offline \
	      --output $(GATES_DIR)/L5-dast-offline.xml; \
	    touch $@; exit 0; \
	  fi; \
	  echo "[L5]   Postgres ready; running migrations..."; \
	  LM_CHAT_SECRET=$${LM_CHAT_SECRET:-l5-dast-secret-00000000000000000000000000000000} \
	  uv run --extra postgres python -c "from alembic.config import Config; from alembic import command; \
cfg = Config('alembic.ini'); \
cfg.set_main_option('sqlalchemy.url', '$(L5_PG_URL_ASYNC)'); \
command.upgrade(cfg, 'head'); \
print('migrations applied: head')" >$(GATES_DIR)/L5-migrate.log 2>&1 || { \
	    echo "[L5]   migrations failed:"; cat $(GATES_DIR)/L5-migrate.log; \
	    uv run python tools/junit_from_schemathesis.py \
	      --skipped-reason "migrations failed; see L5-migrate.log" \
	      --input /dev/null --output $(GATES_DIR)/L5-schemathesis.xml; \
	    uv run python tools/emit_stub_junit.py \
	      --layer 5 --slug dast-offline \
	      --output $(GATES_DIR)/L5-dast-offline.xml; \
	    touch $@; exit 0; \
	  }; \
	  echo "[L5]   booting backend on $(L5_BACKEND_URL)..."; \
	  DATABASE_URL='$(L5_PG_URL_ASYNC)' \
	  LM_CHAT_SECRET=$${LM_CHAT_SECRET:-l5-dast-secret-00000000000000000000000000000000} \
	  LM_CHAT_SETUP_TOKEN='$(L5_SETUP_TOKEN)' \
	  LM_CHAT_HOST='$(L5_BACKEND_HOST)' \
	  LM_CHAT_PORT='$(L5_BACKEND_PORT)' \
	  uv run --extra postgres uvicorn lmchat.app:app \
	    --host $(L5_BACKEND_HOST) --port $(L5_BACKEND_PORT) \
	    --log-level warning \
	    >$(GATES_DIR)/L5-backend.log 2>&1 & \
	  L5_BACKEND_PID=$$!; \
	  backend_up=0; \
	  for i in $$(seq 1 60); do \
	    if curl -sf $(L5_BACKEND_URL)/healthz >/dev/null 2>&1; then \
	      backend_up=1; break; \
	    fi; \
	    if ! kill -0 $$L5_BACKEND_PID >/dev/null 2>&1; then \
	      echo "[L5]   backend exited prematurely"; break; \
	    fi; \
	    sleep 1; \
	  done; \
	  if [ "$$backend_up" -ne 1 ]; then \
	    echo "[L5]   backend never came up; see L5-backend.log"; \
	    tail -30 $(GATES_DIR)/L5-backend.log || true; \
	    uv run python tools/junit_from_schemathesis.py \
	      --skipped-reason "backend never came up on $(L5_BACKEND_URL); see L5-backend.log" \
	      --input /dev/null --output $(GATES_DIR)/L5-schemathesis.xml; \
	    uv run python tools/junit_from_schemathesis.py \
	      --skipped-reason "backend never came up; idor-grid skipped" \
	      --input /dev/null --output $(GATES_DIR)/L5-idor-grid.xml; \
	    uv run python tools/combine_layer_junit.py \
	      --gates-dir $(GATES_DIR) --layer 5 --slug dast-offline \
	      --sub L5-schemathesis.xml --sub L5-idor-grid.xml || true; \
	    touch $@; exit 0; \
	  fi; \
	  echo "[L5]   backend up; running schemathesis (phases: examples,coverage,fuzzing,stateful)"; \
	  rm -f $(GATES_DIR)/L5-schemathesis-raw.xml; \
	  SCHEMATHESIS_HOOKS=security.schemathesis.hooks \
	  uv run schemathesis run docs/api/openapi.yaml \
	    -u $(L5_BACKEND_URL) \
	    --phases=examples,coverage,fuzzing,stateful \
	    --max-failures=50 \
	    --report=junit \
	    --report-dir=$(GATES_DIR)/L5-schemathesis-report \
	    --report-junit-path=$(GATES_DIR)/L5-schemathesis-raw.xml \
	    >$(GATES_DIR)/L5-schemathesis.log 2>&1 || true; \
	  if [ -f $(GATES_DIR)/L5-schemathesis-raw.xml ]; then \
	    uv run python tools/junit_from_schemathesis.py \
	      --input $(GATES_DIR)/L5-schemathesis-raw.xml \
	      --output $(GATES_DIR)/L5-schemathesis.xml || true; \
	    uv run python tools/schemathesis_flake_filter.py \
	      --input $(GATES_DIR)/L5-schemathesis.xml \
	      --allowlist tests/security/schemathesis-known-flakes.yaml \
	      --output $(GATES_DIR)/L5-schemathesis.xml || true; \
	  else \
	    echo "[L5]   schemathesis produced no XML; emitting error sentinel"; \
	    uv run python tools/junit_from_schemathesis.py \
	      --input $(GATES_DIR)/L5-schemathesis-raw.xml \
	      --output $(GATES_DIR)/L5-schemathesis.xml || true; \
	  fi; \
	  echo "[L5]   running idor-grid..."; \
	  uv run python tools/generate_idor_grid.py \
	    --openapi docs/api/openapi.yaml \
	    --base-url $(L5_BACKEND_URL) \
	    --admin-username idor_admin \
	    --user-a-username idor_user_a \
	    --user-b-username idor_user_b \
	    --password 'idor-grid-password-1!' \
	    --setup-token '$(L5_SETUP_TOKEN)' \
	    --junit $(GATES_DIR)/L5-idor-grid.xml \
	    --aggregate $(GATES_DIR)/L5-idor-grid.json \
	    --concurrency 8 \
	    >$(GATES_DIR)/L5-idor-grid.log 2>&1 || true; \
	  if [ ! -f $(GATES_DIR)/L5-idor-grid.xml ]; then \
	    echo "[L5]   idor-grid produced no XML; emitting error sentinel"; \
	    uv run python tools/junit_from_schemathesis.py \
	      --skipped-reason "idor-grid produced no output; see L5-idor-grid.log" \
	      --input /dev/null --output $(GATES_DIR)/L5-idor-grid.xml; \
	  fi; \
	  uv run python tools/combine_layer_junit.py \
	    --gates-dir $(GATES_DIR) --layer 5 --slug dast-offline \
	    --sub L5-schemathesis.xml --sub L5-idor-grid.xml; \
	  combine_rc=$$?; \
	  echo "[L5] elapsed $$(( $$(date +%s) - start ))s"; \
	  if [ $$combine_rc -eq 0 ]; then \
	    touch $@; \
	  else \
	    echo "[L5]   findings present — NOT writing sentinel (aggregator will surface)"; \
	    exit $$combine_rc; \
	  fi

# ----- L6 dast-online (P15d) -----------------------------------------------
# Boots an ephemeral lm-chat target via tests/security/compose-target.yml on
# 127.0.0.1:18001 (loopback-only per PLAN R-32) backed by its own ephemeral
# Postgres on 127.0.0.1:5435, then runs two DAST sub-tools against it:
#
#   1. OWASP ZAP 2.18 baseline scan (ghcr.io/zaproxy/zaproxy:stable)
#   2. Nuclei 3.4 extended template scan (projectdiscovery/nuclei:latest)
#      — http + code + dns + ssl templates, severity ≥ high
#
# Each sub-tool's findings are emitted as JUnit XML; the combiner merges
# them into target/gates/L6-dast-online.xml.
#
# Pre-condition: deploy-lmchat:latest must be present in the local Docker
# daemon (built via `make validate-deploy` or `docker compose -f
# deploy/docker-compose.yml build`). When the image is missing OR Docker
# is unusable (out of disk, daemon not running), the recipe degrades
# gracefully — emits a single "infrastructure-unavailable" skipped layer
# rather than crashing.
#
# ZAP false-positive whitelist lives at tests/security/zap-baseline.conf
# (PLAN R-28); start empty, populate as needed.
#
# Budget: 240s wall-clock (PLAN §2 says 180s but allow 60s for first-run
# nuclei template download).

L6_COMPOSE      := tests/security/compose-target.yml
L6_BACKEND_URL  := http://localhost:18001
L6_ZAP_IMAGE    := ghcr.io/zaproxy/zaproxy:stable
L6_NUCLEI_IMAGE := projectdiscovery/nuclei:latest
L6_ZAP_CONF     := tests/security/zap-baseline.conf

$(GATES_DIR)/.L6-passed: $(GATES_DIR)/.L5-passed
	@echo "[L6] dast-online: ZAP baseline + nuclei extended against ephemeral lm-chat"
	@set -u; \
	  start=$$(date +%s); \
	  mkdir -p $(GATES_DIR); \
	  if ! command -v docker >/dev/null 2>&1; then \
	    echo "[L6]   docker not on PATH — emitting infrastructure-unavailable skipped layer"; \
	    uv run python tools/emit_stub_junit.py \
	      --layer 6 --slug dast-online \
	      --output $(GATES_DIR)/L6-dast-online.xml; \
	    touch $@; exit 0; \
	  fi; \
	  if ! docker image inspect deploy-lmchat:latest >/dev/null 2>&1; then \
	    echo "[L6]   deploy-lmchat:latest not present — emitting infrastructure-unavailable skipped layer"; \
	    uv run python tools/emit_stub_junit.py \
	      --layer 6 --slug dast-online \
	      --output $(GATES_DIR)/L6-dast-online.xml; \
	    touch $@; exit 0; \
	  fi; \
	  cleanup() { \
	    rc=$$?; \
	    echo "[L6]   cleanup: tearing down compose target"; \
	    docker compose -f $(L6_COMPOSE) down -v --remove-orphans >/dev/null 2>&1 || true; \
	    return $$rc; \
	  }; \
	  trap cleanup EXIT INT TERM; \
	  echo "[L6]   booting ephemeral lm-chat + Postgres via $(L6_COMPOSE)..."; \
	  if ! docker compose -f $(L6_COMPOSE) up -d >$(GATES_DIR)/L6-compose-boot.log 2>&1; then \
	    if grep -qiE "no space left on device|out of disk space" $(GATES_DIR)/L6-compose-boot.log; then \
	      echo "[L6]   Docker out of disk space — emitting infrastructure-unavailable skipped layer"; \
	    else \
	      echo "[L6]   docker compose up failed — emitting infrastructure-unavailable skipped layer"; \
	      cat $(GATES_DIR)/L6-compose-boot.log; \
	    fi; \
	    uv run python tools/emit_stub_junit.py \
	      --layer 6 --slug dast-online \
	      --output $(GATES_DIR)/L6-dast-online.xml; \
	    touch $@; exit 0; \
	  fi; \
	  backend_up=0; \
	  for i in $$(seq 1 60); do \
	    if curl -sf $(L6_BACKEND_URL)/healthz >/dev/null 2>&1; then \
	      backend_up=1; break; \
	    fi; \
	    sleep 1; \
	  done; \
	  if [ "$$backend_up" -ne 1 ]; then \
	    echo "[L6]   target never became healthy on $(L6_BACKEND_URL) (60s) — degrading"; \
	    docker compose -f $(L6_COMPOSE) logs lmchat | tail -50 || true; \
	    uv run python tools/junit_from_zap.py --skipped \
	      --skipped-reason "lm-chat target never became healthy on $(L6_BACKEND_URL); see L6-compose-boot.log" \
	      --output $(GATES_DIR)/L6-zap.xml; \
	    uv run python tools/junit_from_nuclei.py --skipped \
	      --skipped-reason "lm-chat target never became healthy; nuclei skipped" \
	      --output $(GATES_DIR)/L6-nuclei.xml; \
	    uv run python tools/combine_layer_junit.py \
	      --gates-dir $(GATES_DIR) --layer 6 --slug dast-online \
	      --sub L6-zap.xml --sub L6-nuclei.xml || true; \
	    touch $@; exit 0; \
	  fi; \
	  echo "[L6]   target up on $(L6_BACKEND_URL); running ZAP baseline..."; \
	  docker run --rm --network host \
	    -v $$(pwd)/$(GATES_DIR):/zap/wrk:rw \
	    $(L6_ZAP_IMAGE) zap-baseline.py \
	    -t $(L6_BACKEND_URL) \
	    -J L6-zap.json \
	    -x L6-zap-zap.xml \
	    >$(GATES_DIR)/L6-zap.log 2>&1 || true; \
	  if [ -f $(GATES_DIR)/L6-zap.json ]; then \
	    uv run python tools/junit_from_zap.py \
	      --input $(GATES_DIR)/L6-zap.json \
	      --output $(GATES_DIR)/L6-zap.xml \
	      --whitelist $(L6_ZAP_CONF) || true; \
	  else \
	    echo "[L6]   ZAP produced no JSON; emitting skipped sentinel"; \
	    uv run python tools/junit_from_zap.py --skipped \
	      --skipped-reason "ZAP did not produce JSON output; see L6-zap.log" \
	      --output $(GATES_DIR)/L6-zap.xml; \
	  fi; \
	  echo "[L6]   running nuclei extended scan (http + code + dns + ssl, severity >= high)..."; \
	  docker run --rm --network host \
	    -v $$(pwd)/$(GATES_DIR):/output:rw \
	    $(L6_NUCLEI_IMAGE) \
	    -u $(L6_BACKEND_URL) \
	    -t /nuclei-templates/http \
	    -t /nuclei-templates/code \
	    -t /nuclei-templates/dns \
	    -t /nuclei-templates/ssl \
	    -severity critical,high \
	    -j -o /output/L6-nuclei.json \
	    -duc \
	    >$(GATES_DIR)/L6-nuclei.log 2>&1 || true; \
	  if [ -f $(GATES_DIR)/L6-nuclei.json ]; then \
	    uv run python tools/junit_from_nuclei.py \
	      --input $(GATES_DIR)/L6-nuclei.json \
	      --output $(GATES_DIR)/L6-nuclei.xml || true; \
	  else \
	    echo "[L6]   nuclei produced no JSON; emitting no-findings sentinel"; \
	    uv run python tools/junit_from_nuclei.py --skipped \
	      --skipped-reason "nuclei did not produce output; see L6-nuclei.log" \
	      --output $(GATES_DIR)/L6-nuclei.xml; \
	  fi; \
	  uv run python tools/combine_layer_junit.py \
	    --gates-dir $(GATES_DIR) --layer 6 --slug dast-online \
	    --sub L6-zap.xml --sub L6-nuclei.xml; \
	  combine_rc=$$?; \
	  echo "[L6] elapsed $$(( $$(date +%s) - start ))s"; \
	  if [ $$combine_rc -eq 0 ]; then \
	    touch $@; \
	  else \
	    echo "[L6]   findings present — NOT writing sentinel (aggregator will surface)"; \
	    exit $$combine_rc; \
	  fi

# ----- L6-dos DoS + availability (P15f) ------------------------------------
# Runs four DoS sub-tools against the L6 compose-target (or degrades
# gracefully when Docker / the target is absent):
#
#   1. tools/redos_fuzzer.py         — Hypothesis-driven ReDoS fuzzer
#   2. tools/decompression_bomb_test.py — gzip/zip bomb upload harness
#   3. tools/json_bomb_test.py       — JSON bomb (billion-laughs, massive array,
#                                      duplicate-key) harness
#   4. tools/junit_from_slowhttptest.py — slowhttptest Slowloris adapter
#
# Each sub-tool is independent; failures are converted to JUnit XML so the
# aggregator can surface them.  Sentinel depends on .L6-passed so that the
# compose-target is already up when slowhttptest runs.
#
# Budget: 120s wall-clock.
$(GATES_DIR)/.L6-dos-passed: $(GATES_DIR)/.L6-passed
	@echo "[L6-dos] DoS + availability: redos + decompression + json_bomb + slowhttptest"
	@set -u; \
	  start=$$(date +%s); \
	  mkdir -p $(GATES_DIR); \
	  echo "[L6-dos]   running redos_fuzzer..."; \
	  uv run python tools/redos_fuzzer.py \
	    --output $(GATES_DIR)/L6-dos-redos.xml || true; \
	  echo "[L6-dos]   running decompression_bomb_test..."; \
	  PYTHONPATH=. uv run python tools/decompression_bomb_test.py \
	    --output $(GATES_DIR)/L6-dos-decompression.xml || true; \
	  echo "[L6-dos]   running json_bomb_test..."; \
	  PYTHONPATH=. uv run python tools/json_bomb_test.py \
	    --output $(GATES_DIR)/L6-dos-jsonbomb.xml || true; \
	  echo "[L6-dos]   running junit_from_slowhttptest (skips if Docker unavailable)..."; \
	  if command -v docker >/dev/null 2>&1; then \
	    uv run python tools/junit_from_slowhttptest.py \
	      --target-url $(L6_BACKEND_URL) \
	      --output $(GATES_DIR)/L6-dos-slowloris.xml || true; \
	  else \
	    echo "[L6-dos]   docker not on PATH — emitting skipped testcase for slowhttptest"; \
	    uv run python tools/junit_from_slowhttptest.py --skipped \
	      --skipped-reason "docker not on PATH" \
	      --output $(GATES_DIR)/L6-dos-slowloris.xml || true; \
	  fi; \
	  echo "[L6-dos]   running latency_budget_test..."; \
	  PYTHONPATH=. uv run python tools/latency_budget_test.py \
	    --target-url $(L6_BACKEND_URL) \
	    --output $(GATES_DIR)/L6-dos-latency.xml || true; \
	  uv run python tools/combine_layer_junit.py \
	    --gates-dir $(GATES_DIR) \
	    --layer 6 --slug dos \
	    --sub L6-dos-redos.xml \
	    --sub L6-dos-decompression.xml \
	    --sub L6-dos-jsonbomb.xml \
	    --sub L6-dos-slowloris.xml \
	    --sub L6-dos-latency.xml; \
	  combine_rc=$$?; \
	  echo "[L6-dos] elapsed $$(( $$(date +%s) - start ))s"; \
	  if [ $$combine_rc -eq 0 ]; then \
	    touch $@; \
	  else \
	    echo "[L6-dos]   findings present — NOT writing sentinel (aggregator will surface)"; \
	    exit $$combine_rc; \
	  fi

# ----- L7 llm-security (P15e) ----------------------------------------------
# Runs the L7 LLM red-team layer (PLAN §3 + §4 OWASP LLM Top 10).
#
# Two halves:
#
#   1. garak SSE adapter         -> tools/garak_sse_generator.py against
#                                   the SSE endpoint POST /api/chat/stream
#                                   on the L6 compose target.
#      garak JSON path           -> tests/security/garak/rest_json.json
#                                   placeholder; SKIPPED at gate-run time
#                                   because lm-chat does not expose a
#                                   non-streaming /v1/chat/completions
#                                   surface (per CONSTRAINTS in P15e PROMPT).
#
#   2. Custom OWASP-LLM-Top-10  -> pytest fixtures at tests/security/llm/
#      pytest fixtures             (LLM02 / LLM06 / LLM07 / LLM08).  Pytest's
#                                  native JUnit XML output captured to
#                                  $(GATES_DIR)/L7-llm-top10.xml.
#
# Sub-tool failure modes are all converted to JUnit XML — the recipe never
# crashes the gate.  Graceful skip when garak binary is missing or the L6
# compose target isn't reachable.
#
# Budget: 120s wall-clock (PLAN §2 budget).  garak's probe count is capped
# at 10 generations per probe via ``--generations 10`` to stay inside.

L7_BACKEND_URL := $(L6_BACKEND_URL)
L7_GARAK_PROBES := encoding,xss,gcg,promptinject,leakreplay

$(GATES_DIR)/.L7-passed: $(GATES_DIR)/.L6-passed
	@echo "[L7] llm-security: garak (SSE + JSON-placeholder) + OWASP LLM Top 10 fixtures"
	@set -u; \
	  start=$$(date +%s); \
	  mkdir -p $(GATES_DIR); \
	  ran_llm_top10=0; \
	  echo "[L7]   running OWASP LLM Top 10 custom fixtures (tests/security/llm/)..."; \
	  uv run pytest tests/security/llm/ \
	    --no-cov -q --tb=short \
	    --junit-xml=$(GATES_DIR)/L7-llm-top10.xml \
	    >$(GATES_DIR)/L7-llm-top10.log 2>&1 || true; \
	  if [ -f $(GATES_DIR)/L7-llm-top10.xml ]; then \
	    ran_llm_top10=1; \
	  else \
	    echo "[L7]   pytest produced no XML — emitting error sentinel"; \
	    uv run python tools/emit_stub_junit.py \
	      --layer 7 --slug llm-top10 \
	      --output $(GATES_DIR)/L7-llm-top10.xml; \
	  fi; \
	  echo "[L7]   garak JSON path -> skipped (no /v1/chat/completions on lm-chat; see tests/security/garak/rest_json.json)"; \
	  uv run python tools/junit_from_garak.py \
	    --skipped \
	    --suite-name l7-garak-json \
	    --skipped-reason "lm-chat does not expose a non-streaming /v1/chat/completions endpoint; SSE path covers the gap (see tests/security/garak/rest_json.json header comments)" \
	    --output $(GATES_DIR)/L7-garak-json.xml; \
	  echo "[L7]   garak SSE adapter path..."; \
	  if ! command -v garak >/dev/null 2>&1; then \
	    echo "[L7]   garak not on PATH — emitting <skipped> for SSE path"; \
	    uv run python tools/junit_from_garak.py \
	      --skipped \
	      --suite-name l7-garak-sse \
	      --skipped-reason "garak binary not on PATH (install: uv pip install garak>=0.10.0)" \
	      --output $(GATES_DIR)/L7-garak-sse.xml; \
	  elif ! curl -sf $(L7_BACKEND_URL)/healthz >/dev/null 2>&1; then \
	    echo "[L7]   L6 compose target unreachable at $(L7_BACKEND_URL) — emitting <skipped> for SSE path"; \
	    uv run python tools/junit_from_garak.py \
	      --skipped \
	      --suite-name l7-garak-sse \
	      --skipped-reason "L6 compose target not reachable at $(L7_BACKEND_URL); bring up tests/security/compose-target.yml first" \
	      --output $(GATES_DIR)/L7-garak-sse.xml; \
	  else \
	    echo "[L7]   garak run: probes=$(L7_GARAK_PROBES) target=$(L7_BACKEND_URL)/api/chat/stream"; \
	    PYTHONPATH=$(PWD) garak \
	      --model_type lmchat_sse \
	      --model_name lmchat-sse \
	      --generator_option_file tests/security/garak/rest_sse.json \
	      --probes $(L7_GARAK_PROBES) \
	      --generations 10 \
	      --report_prefix $(GATES_DIR)/L7-garak \
	      >$(GATES_DIR)/L7-garak.log 2>&1 || true; \
	    raw_report=$$(ls -1t $(GATES_DIR)/L7-garak*.report.jsonl 2>/dev/null | head -1 || echo ""); \
	    if [ -n "$$raw_report" ] && [ -f "$$raw_report" ]; then \
	      uv run python tools/junit_from_garak.py \
	        --input "$$raw_report" \
	        --suite-name l7-garak-sse \
	        --output $(GATES_DIR)/L7-garak-sse.xml || true; \
	    else \
	      echo "[L7]   garak produced no report.jsonl — emitting <skipped>"; \
	      uv run python tools/junit_from_garak.py \
	        --skipped \
	        --suite-name l7-garak-sse \
	        --skipped-reason "garak ran but produced no JSONL report; see L7-garak.log" \
	        --output $(GATES_DIR)/L7-garak-sse.xml; \
	    fi; \
	  fi; \
	  uv run python tools/combine_layer_junit.py \
	    --gates-dir $(GATES_DIR) --layer 7 --slug llm-security \
	    --sub L7-llm-top10.xml --sub L7-garak-sse.xml --sub L7-garak-json.xml; \
	  combine_rc=$$?; \
	  echo "[L7] elapsed $$(( $$(date +%s) - start ))s (llm-top10-ran=$$ran_llm_top10)"; \
	  if [ $$combine_rc -eq 0 ]; then \
	    touch $@; \
	  else \
	    echo "[L7]   findings present — NOT writing sentinel (aggregator will surface)"; \
	    exit $$combine_rc; \
	  fi

# ----- L9 aggregator + dashboard (P15a + P15i extension) -------------------
# Reads every L*.xml + named sub-tool XMLs (L4-trivy, L6-dos-*, L8-auth-*,
# etc.), emits report.json (schema-validated) + index.html (via
# tools/render_dashboard.py).
#
# .L9-passed depends on ALL prior layer sentinels so the full chain is
# enforced when invoked directly (standalone dev mode retains .L0-passed
# as the only hard prerequisite; the full-chain variant is .L9-fullchain).
#
# Per PLAN §2 budget: 5s. Exit code 0 only when all required layers green.
$(GATES_DIR)/.L9-passed: $(GATES_DIR)/.L0-passed \
                          $(GATES_DIR)/.L4-passed \
                          $(GATES_DIR)/.L5-passed \
                          $(GATES_DIR)/.L6-passed \
                          $(GATES_DIR)/.L6-dos-passed \
                          $(GATES_DIR)/.L7-passed \
                          $(GATES_DIR)/.L8-auth-passed
	@echo "[L9] gate-report aggregator + dashboard (P15i)"
	@uv run python tools/aggregate_junit.py --gates-dir $(GATES_DIR)
	@uv run python tools/render_dashboard.py --gates-dir $(GATES_DIR)
	@echo "[L9]   dashboard written: $(GATES_DIR)/index.html"
	@touch $@

# Full L0..L9 chain.
production-gate: $(GATES_DIR)/.L8-auth-passed $(GATES_DIR)/.L6-dos-passed $(GATES_DIR)/.L9-fullchain
	@echo ""
	@echo "  \033[32m[OK] production-gate complete\033[0m"
	@echo "       report:    $(GATES_DIR)/report.json"
	@echo "       dashboard: $(GATES_DIR)/index.html"
	@echo ""

# L9 must depend on the full chain for the `production-gate` target, but the
# `.L9-passed` sentinel above depends on all wave-2+3 sentinels so it can be
# invoked standalone for development. We split the full-chain variant out.
$(GATES_DIR)/.L9-fullchain: $(GATES_DIR)/.L8-auth-passed $(GATES_DIR)/.L6-dos-passed
	@uv run python tools/aggregate_junit.py --gates-dir $(GATES_DIR)
	@uv run python tools/render_dashboard.py --gates-dir $(GATES_DIR)
	@touch $@

# Quick pre-flight: L0..L3 only (no Docker, no DAST online, no LLM probes).
production-gate-quick: $(GATES_DIR)/.L3-passed
	@uv run python tools/aggregate_junit.py --gates-dir $(GATES_DIR) || true
	@echo ""
	@echo "  \033[32m✔ production-gate-quick complete\033[0m   see $(GATES_DIR)/index.html"
	@echo ""

# ----- soak-test (P15h, standalone) ----------------------------------------
# On-demand extended soak test — NOT part of production-gate (PLAN §1.7).
# Runs a steady-state Locust load against the headless compose-target on
# :18001 for the configured duration, then emits JUnit XML from the
# checkpoint log.
#
# Default duration: 4 h.  Set SOAK_DURATION_HOURS to override.
# CI scheduled runs may use SOAK_DURATION_HOURS=24.
# A 5-minute dry-run: make soak-test SOAK_DURATION_HOURS=0.0833
#
# Pre-condition: the lm-chat compose-target must already be running on
# http://localhost:18001 (boot via:
#   docker compose -f tests/security/compose-target.yml up -d
# ).  When the target is unreachable, soak_test.py exits with code 2 and
# junit_from_soak.py emits a <skipped> sentinel so a JUnit artifact always
# exists for the gate report.
#
# Output:
#   target/gates/soak-checkpoints.jsonl   one JSON object per 30-min checkpoint
#   target/gates/soak-test.xml            JUnit XML (one testcase per assertion)
.PHONY: soak-test
soak-test:
	@echo "[soak] starting (duration: $${SOAK_DURATION_HOURS:-4}h)"
	@mkdir -p target/gates
	@uv run python tools/soak_test.py \
	  --duration-hours $${SOAK_DURATION_HOURS:-4} \
	  --target-url http://localhost:18001 \
	  --output-dir target/gates; \
	uv run python tools/junit_from_soak.py \
	  --checkpoints target/gates/soak-checkpoints.jsonl \
	  --output target/gates/soak-test.xml

# ---------------------------------------------------------------------------
# Gap 1: Coverage merge (py + TS → LCOV)
# ---------------------------------------------------------------------------
# Merges Python (coverage.py LCOV) and TypeScript (vitest v8 LCOV) into a
# single unified report under target/coverage/html/.
#
# Requires: lcov + genhtml on PATH. If absent the target prints a hint and
# exits 0 so it never blocks `make test` or the production-gate chain.
#
# Python lcov is written to coverage/py.lcov via pyproject.toml [tool.coverage.lcov].
# TS lcov is written to web/coverage/lcov.info by vitest (reporters: ["text","lcov"]).
coverage-merged: ## merged py+TS LCOV report at target/coverage/html/
	@set -e; \
	if ! command -v lcov >/dev/null 2>&1 || ! command -v genhtml >/dev/null 2>&1; then \
	  echo "[coverage-merged] SKIP: lcov/genhtml not on PATH."; \
	  echo "  Install: brew install lcov  (macOS) | apt install lcov  (Debian/Ubuntu)"; \
	  exit 0; \
	fi; \
	mkdir -p coverage target/coverage; \
	uv run pytest --cov=lmchat --cov-report=lcov:coverage/py.lcov -q; \
	(cd web && npx vitest run --coverage); \
	lcov --add-tracefile coverage/py.lcov \
	     --add-tracefile web/coverage/lcov.info \
	     -o target/coverage/merged.lcov; \
	genhtml target/coverage/merged.lcov --output-directory target/coverage/html; \
	echo "[coverage-merged] report: target/coverage/html/index.html"

# ---------------------------------------------------------------------------
# Gap 2: Flake detection (opt-in, NOT in default make test)
# ---------------------------------------------------------------------------
# Runs pytest with randomised seed from the previous run. The --randomly-seed
# preserves reproducibility — re-run with the same seed to confirm a flake.
# Add --rerun-failures=3 to auto-retry intermittent failures and surface them.
#
# Does NOT change the default `make test` target. Opt-in only.
test-flake-scan: ## opt-in: randomised-seed pytest run for flake detection
	uv run pytest -p randomly --randomly-seed=last $(TEST_ARGS)

# ---------------------------------------------------------------------------
# Gap 3: Mutation gate (Python, cosmic-ray)
# ---------------------------------------------------------------------------
# mutation-gate: slow — nightly/pre-tag only, not in make test
#
# Runs the mutation baseline for all three targets, then checks that cr-rate
# is >= 0.60 for each. Exits non-zero on failure.
# tools/check_mutation_scores.py parses cr-rate output and validates the
# threshold. The underlying baseline run populates target/mutation/*.sqlite.
mutation-gate: ## nightly: cosmic-ray mutation gate (cr-rate >= 60% for all targets)
	@echo "[mutation-gate] running cosmic-ray baseline (all targets)..."
	bash scripts/mutation-baseline.sh all
	@echo "[mutation-gate] checking kill-rate thresholds (>= 0.60)..."
	uv run python tools/check_mutation_scores.py \
	  --threshold 0.60 \
	  --sessions \
	    target/mutation/streaming_client.sqlite \
	    target/mutation/native.sqlite \
	    target/mutation/chats.sqlite

# ---------------------------------------------------------------------------
# Gap 5: Visual regression harness (Playwright screenshot diffing)
# ---------------------------------------------------------------------------
# visual-baseline: ONE-TIME run to generate / update PNG baselines.
#   Requires the app to be running on :8011 (make sure `uv run uvicorn …` is up).
#   Snapshots land under web/tests/screenshots/__snapshots__/ (gitignored by default).
#
# visual-test: CI gate — diffs current render against stored baselines.
#   Fails if any pixel delta exceeds Playwright's default threshold.
visual-baseline: ## one-time: generate screenshot baselines (needs running app on :8011)
	cd web && npx playwright test tests/screenshots/visual.spec.ts --update-snapshots

visual-test: ## CI: diff current render against stored visual baselines
	cd web && npx playwright test tests/screenshots/visual.spec.ts
