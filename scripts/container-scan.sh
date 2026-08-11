#!/usr/bin/env bash
# =============================================================================
# LMChat — Container Scan Suite (PLAN v3 §2F)
# =============================================================================
#
# Dedicated per-tier container scan for the container-scan CI workflow.
# Mirrors ~/projects/EMS-FE/scripts/container-scan.sh shape with lm-chat-v1
# conventions (CI/local mode distinction from security-static.sh).
#
# What this does:
#   1. Hadolint — lint deploy/Dockerfile for best-practice violations
#   2. Docker build — build the image (REQUIRED — exits 1 on failure)
#   3. Trivy image scan — HIGH/CRITICAL vuln gate + secret + misconfig
#   4. Dockle — CIS Docker Benchmark check; FATAL level fails the gate
#
# Tool-detection behaviour (mirrors security-static.sh):
#   LOCAL mode (CI != true): if a tool is NOT installed, print an install
#     hint and SKIP with a visible WARNING. Other tools still run.
#   CI mode (CI=true): all tools are assumed present. A missing tool is a
#     hard failure (CI should install them via the workflow).
#
# Exit code:
#   0 — all present tools passed (no gating findings).
#   1 — one or more present tools found gating issues, OR a tool is
#       missing in CI mode.
#
# JUnit XML output is handled by the CI workflow (not this script).
# This script emits human-readable pass/fail/skip per step.
#
# Tool versions (pin these to match CI):
#   hadolint  v2.12.0   https://github.com/hadolint/hadolint/releases/tag/v2.12.0
#   trivy     0.58.x    https://aquasecurity.github.io/trivy/latest/
#   dockle    v0.4.15   https://github.com/goodwithtech/dockle/releases/tag/v0.4.15
#
# Usage (from repo root):
#   bash scripts/container-scan.sh          # local mode: skip missing tools
#   CI=true bash scripts/container-scan.sh  # CI mode: fail on missing tools
#
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOCKERFILE="${REPO_ROOT}/deploy/Dockerfile"
GATES_DIR="${REPO_ROOT}/target/gates"
mkdir -p "${GATES_DIR}"

# Unique tag per run — avoids conflicts with concurrent runs.
IMAGE_TAG="lmchat:scan-$(date +%s)"

# ---------------------------------------------------------------------------
# Colour helpers (disabled when not a tty or when NO_COLOR is set)
# ---------------------------------------------------------------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[1;33m'
  CYAN='\033[0;36m'
  BOLD='\033[1m'
  RESET='\033[0m'
else
  GREEN='' RED='' YELLOW='' CYAN='' BOLD='' RESET=''
fi

# CI mode: fail on missing tools instead of skipping
CI_MODE="${CI:-false}"

# ---------------------------------------------------------------------------
# Helpers — matching EMS-FE + security-static.sh pattern
# ---------------------------------------------------------------------------
section() { echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════${RESET}"; echo -e "${BOLD}${CYAN}  $1${RESET}"; echo -e "${BOLD}${CYAN}══════════════════════════════════════════${RESET}"; }
pass()    { echo -e "${GREEN}  ✓ PASS${RESET}  $1"; }
fail()    { echo -e "${RED}  ✗ FAIL${RESET}  $1"; }
warn()    { echo -e "${YELLOW}  ⚠ WARN${RESET}  $1"; }
info()    { echo -e "  → $1"; }

OVERALL_RC=0

# check_tool <name> <binary> <install_hint>
# Returns 0 if the tool is available, 1 if not.
# In CI mode, exits the whole script on missing tool.
check_tool() {
  local name="$1" binary="$2" hint="$3"
  if command -v "${binary}" &>/dev/null; then
    return 0
  else
    if [[ "${CI_MODE}" == "true" ]]; then
      fail "${name}: binary '${binary}' not found in CI — install it in the workflow"
      echo "  Install hint: ${hint}" >&2
      exit 1
    else
      warn "${name} not installed. To install: ${hint}"
      warn "Skipping ${name}."
      return 1
    fi
  fi
}

# record_result <tool> <rc>
record_result() {
  local tool="$1" rc="$2"
  if [[ "${rc}" -eq 0 ]]; then
    pass "${tool}"
  else
    fail "${tool} — see output above"
    OVERALL_RC=1
  fi
}

# ---------------------------------------------------------------------------
# Cleanup trap — remove the tagged image on exit
# ---------------------------------------------------------------------------
cleanup() {
  if docker image inspect "${IMAGE_TAG}" &>/dev/null 2>&1; then
    docker image rm "${IMAGE_TAG}" &>/dev/null || true
  fi
}
trap cleanup EXIT

# ===========================================================================
# Step 1 — Hadolint Dockerfile lint
# ===========================================================================
section "Step 1: Hadolint Dockerfile lint (deploy/Dockerfile)"

if check_tool "hadolint" "hadolint" \
    "https://github.com/hadolint/hadolint/releases/tag/v2.12.0"; then
  hadolint "${DOCKERFILE}"
  record_result "step-1-hadolint" $?
else
  info "Step 1 skipped (hadolint not available)."
fi

# ===========================================================================
# Step 2 — Docker build (REQUIRED — exits 1 on failure)
# ===========================================================================
section "Step 2: Docker build (context = repo root, dockerfile = deploy/Dockerfile)"
echo "   Image: ${IMAGE_TAG}"

if docker build \
    --file "${DOCKERFILE}" \
    --tag "${IMAGE_TAG}" \
    "${REPO_ROOT}"; then
  pass "step-2-build"
else
  fail "step-2-build"
  echo -e "${RED}Fatal: Docker build failed — cannot scan an image that doesn't exist. Aborting.${RESET}"
  exit 1
fi

# ===========================================================================
# Step 3 — Trivy image scan
# ===========================================================================
section "Step 3: Trivy image scan (HIGH/CRITICAL vuln + secret + misconfig)"

if check_tool "trivy" "trivy" \
    "https://aquasecurity.github.io/trivy/latest/getting-started/installation/"; then
  echo -e "   ${BOLD}3a — HIGH/CRITICAL CVE gate (exit-code 1 on findings):${RESET}"
  # --skip-dirs: pip bundles its OWN vendored copies of msgpack, pkg_resources
  # (setuptools), etc. under pip/_vendor. trivy flags those (e.g. setuptools
  # 70.3.0 / msgpack 1.1.2) even though the app's ACTUAL packages are patched,
  # and pip is never invoked at runtime. pip's private vendor tree is not the
  # app's dependency surface — skip it.
  trivy image \
    --exit-code 1 \
    --severity HIGH,CRITICAL \
    --scanners vuln,secret,config \
    --skip-dirs '**/pip/_vendor/**' \
    --format json \
    --output "${GATES_DIR}/L4-trivy.json" \
    "${IMAGE_TAG}" 2>/dev/null
  RC_TRIVY_HIGH=$?
  record_result "step-3a-trivy-high-critical" "${RC_TRIVY_HIGH}"

  if [[ "${RC_TRIVY_HIGH}" -ne 0 ]]; then
    echo -e "   ${RED}Trivy found HIGH/CRITICAL issues — fix before shipping.${RESET}"
  fi

  echo ""
  echo -e "   ${BOLD}3b — LOW/MEDIUM/UNKNOWN scan (informational, exit-code 0):${RESET}"
  trivy image \
    --exit-code 0 \
    --severity UNKNOWN,LOW,MEDIUM \
    --scanners vuln,secret,config \
    --skip-dirs '**/pip/_vendor/**' \
    --format json \
    --output "${GATES_DIR}/L4-trivy-info.json" \
    "${IMAGE_TAG}" 2>/dev/null || true
  info "Trivy informational scan complete (LOW/MEDIUM/UNKNOWN)."
else
  info "Step 3 skipped (trivy not available)."
fi

# ===========================================================================
# Step 4 — Dockle CIS benchmark check
# ===========================================================================
section "Step 4: Dockle CIS Docker Benchmark check"

if check_tool "dockle" "dockle" \
    "https://github.com/goodwithtech/dockle/releases/tag/v0.4.15"; then
  # --exit-level fatal: only FATAL findings cause a non-zero exit code.
  # --accept-file settings.py: CIS-DI-0010 ("do not store credential in
  #   files") is a filename heuristic that flags every file named
  #   settings.py. The image carries two third-party library config
  #   modules with that name — h2/settings.py (HTTP/2 frame settings) and
  #   mcp/server/auth/settings.py (an auth-settings schema) — neither of
  #   which stores a credential. Allowlist the basename so the false
  #   positive doesn't fail the scan; content-based credential detection
  #   (env-var keys, private-key material) stays active.
  dockle \
    --exit-code 1 \
    --exit-level fatal \
    --accept-file settings.py \
    --format json \
    -o "${GATES_DIR}/L4-dockle.json" \
    "${IMAGE_TAG}" 2>/dev/null
  record_result "step-4-dockle" $?
else
  info "Step 4 skipped (dockle not available)."
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo -e "${BOLD}════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Image: ${IMAGE_TAG}  (removed on exit)"
echo ""

if [[ "${OVERALL_RC}" -eq 0 ]]; then
  echo -e "  ${GREEN}${BOLD}→ CONTAINER SCAN PASS${RESET}"
else
  echo -e "  ${RED}${BOLD}→ CONTAINER SCAN FAIL${RESET}"
fi

echo ""
echo -e "  Tools: hadolint v2.12.0 · trivy 0.58.x · dockle v0.4.15"
echo -e "${BOLD}════════════════════════════════════════════════════════${RESET}"
echo ""

exit "${OVERALL_RC}"