#!/usr/bin/env bash
# LMChat mutation testing baseline.
#
# Runs cosmic-ray against three target files sequentially.
# Results land in target/mutation/<target>.sqlite.
# HTML reports land in target/mutation/<target>.html.
#
# Usage:
#   scripts/mutation-baseline.sh              # all three targets
#   scripts/mutation-baseline.sh streaming    # streaming_client only
#   scripts/mutation-baseline.sh chats        # chats.py only
#   scripts/mutation-baseline.sh native       # native.py only
#
# Collects baseline scores; not wired into CI. Run after any
# significant change to the three target files to see if coverage improved.
#
# Future: add `cr-rate` threshold check and fail on regression.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MUTATION_DIR="$REPO_ROOT/target/mutation"
mkdir -p "$MUTATION_DIR"

# 4-hour per-file wall-clock budget (cosmic-ray --timeout is per-mutation
# test run; total runtime depends on mutation count × timeout).
TIMEOUT_PER_MUTATION=30

# Test command shared across all targets.
TEST_CMD="uv run pytest tests/services tests/lmstudio tests/routes tests/contracts -x -q -m 'not stress_invariant' --timeout=25"

# ---------------------------------------------------------------------------
# Helper: write a target-specific config TOML, init + run + report
# ---------------------------------------------------------------------------
run_target() {
    local name="$1"
    local module_path="$2"

    local cfg="$MUTATION_DIR/${name}.toml"
    local session="$MUTATION_DIR/${name}.sqlite"
    local report="$MUTATION_DIR/${name}.html"

    echo ""
    echo "============================================================"
    echo "  cosmic-ray: $name  →  $module_path"
    echo "============================================================"

    # Write a minimal per-target config.
    cat > "$cfg" <<TOML
[cosmic-ray]
module-path = "$module_path"
timeout = $TIMEOUT_PER_MUTATION
excluded-modules = []
test-command = "$TEST_CMD"

[cosmic-ray.distributor]
name = "local"
TOML

    echo "[baseline] initialising session: $session"
    uv run cosmic-ray init "$cfg" "$session"

    echo "[baseline] running mutations (this will take a while)..."
    uv run cosmic-ray exec "$cfg" "$session"

    echo "[baseline] generating HTML report: $report"
    uv run cr-html "$session" > "$report"

    echo "[baseline] summary for $name:"
    uv run cr-rate "$session" || true   # cr-rate exits non-zero when score < threshold; ignore for baseline collection
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
TARGET="${1:-all}"

case "$TARGET" in
    streaming|streaming_client)
        run_target "streaming_client" "src/lmchat/services/lmstudio_streaming_client.py"
        ;;
    chats)
        # Note: cosmic-ray v8 does not support function-level scoping.
        # The full chats.py is mutated; test suite is filtered to sub-session tests
        # to keep the run manageable given chats.py is the largest file.
        run_target "chats" "src/lmchat/routes/chats.py"
        ;;
    native)
        run_target "native" "src/lmchat/lmstudio/native.py"
        ;;
    all)
        run_target "streaming_client" "src/lmchat/services/lmstudio_streaming_client.py"
        run_target "native"           "src/lmchat/lmstudio/native.py"
        run_target "chats"            "src/lmchat/routes/chats.py"
        ;;
    *)
        echo "Unknown target: $TARGET" >&2
        echo "Usage: $0 [streaming|chats|native|all]" >&2
        exit 1
        ;;
esac

echo "Baseline complete. Reports in $MUTATION_DIR/"
