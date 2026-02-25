#!/usr/bin/env bash
# Nexus E2E test suite — runs Claude Code in a devcontainer and exercises
# nx CLI, plugin skills, sequential thinking, and compaction resilience.
#
# Usage:
#   ./tests/e2e/run.sh                   # run all scenarios
#   ./tests/e2e/run.sh --no-build        # skip docker build (reuse existing image)
#   ./tests/e2e/run.sh --scenario 02     # run a single scenario by number
#
# Prerequisites:
#   - Docker running
#   - .env file at repo root with ANTHROPIC_API_KEY, VOYAGE_API_KEY, CHROMA_* set
#   - (macOS) Docker Desktop or OrbStack

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONTAINER="nexus-e2e"
IMAGE="nexus-e2e-img"
BUILD=true
ONLY_SCENARIO=""

# ─── Argument parsing ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-build)   BUILD=false; shift ;;
        --scenario)   ONLY_SCENARIO="$2"; shift 2 ;;
        *)            echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ─── Load credentials ─────────────────────────────────────────────────────────

if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a; source "$REPO_ROOT/.env"; set +a
fi

: "${ANTHROPIC_API_KEY:?'ANTHROPIC_API_KEY must be set (in .env or environment)'}"

# ─── Source helpers ───────────────────────────────────────────────────────────

source "$SCRIPT_DIR/lib.sh"

# ─── Cleanup on exit ──────────────────────────────────────────────────────────

cleanup() {
    echo ""
    echo "Cleaning up container..."
    docker rm -f "$CONTAINER" 2>/dev/null || true
}
trap cleanup EXIT

# ─── Build image ──────────────────────────────────────────────────────────────

if [[ "$BUILD" == true ]]; then
    echo "Building nexus e2e image (this takes a few minutes on first run)..."
    docker build \
        -f "$REPO_ROOT/.devcontainer/Dockerfile" \
        -t "$IMAGE" \
        "$REPO_ROOT"
    echo "Image built: $IMAGE"
fi

# ─── Start container ──────────────────────────────────────────────────────────

echo "Starting container..."
docker run -d --name "$CONTAINER" \
    -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" \
    -e VOYAGE_API_KEY="${VOYAGE_API_KEY:-}" \
    -e CHROMA_API_KEY="${CHROMA_API_KEY:-}" \
    -e CHROMA_TENANT="${CHROMA_TENANT:-}" \
    -e CHROMA_DATABASE="${CHROMA_DATABASE:-default_database}" \
    -v "$REPO_ROOT:/workspace" \
    "$IMAGE" \
    sleep infinity

# Copy host ~/.claude.json (contains oauthAccount session token) into the
# container so Claude Code can authenticate without an interactive OAuth flow.
if [[ -f "$HOME/.claude.json" ]]; then
    docker cp "$HOME/.claude.json" "$CONTAINER:/home/node/.claude.json"
    echo "Auth state copied from host."
else
    echo "WARNING: ~/.claude.json not found — Claude interactive mode may prompt for OAuth."
fi

# Suppress zsh new-user wizard (would absorb keystrokes before Claude starts)
crun "touch /home/node/.zshrc"

# Start tmux session inside the container
cexec tmux new-session -d -s e2e -x 220 -y 50

# ─── Install conexus from workspace ──────────────────────────────────────────
# Reinstall from the mounted workspace so we always run local code,
# not whatever version was baked into the image from PyPI.

echo "Installing conexus from workspace..."
crun "uv tool install /workspace --force 2>&1 | tail -5"

# ─── Install nx plugin ────────────────────────────────────────────────────────
# The nx plugin lives at /workspace/nx (the nx/ directory in the repo).
# claude plugin install accepts a path to a plugin directory or a
# marketplace.json. Try the marketplace path first.

echo "Installing nx plugin..."
crun "claude plugin install /workspace 2>&1 || \
      claude plugin install /workspace/nx 2>&1 || \
      echo 'PLUGIN_INSTALL_FAILED'" | tee /tmp/plugin-install.log

if grep -q "PLUGIN_INSTALL_FAILED" /tmp/plugin-install.log; then
    echo "WARNING: Automated plugin install failed — trying manual copy..."
    # Fallback: manually symlink nx plugin into ~/.claude
    crun "mkdir -p /home/node/.claude/plugins && \
          ln -sfn /workspace/nx /home/node/.claude/plugins/nx"
    echo "Manual plugin symlink created."
fi

echo "Plugin install complete."

# ─── Run scenarios ────────────────────────────────────────────────────────────

run_scenario() {
    local file="$1"
    local num
    num=$(basename "$file" | cut -d_ -f1)
    if [[ -n "$ONLY_SCENARIO" && "$num" != "$ONLY_SCENARIO" ]]; then
        return 0
    fi
    echo ""
    echo "══════════════════════════════════════════════"
    # shellcheck source=/dev/null
    source "$file"
}

for scenario_file in "$SCRIPT_DIR"/scenarios/[0-9]*.sh; do
    run_scenario "$scenario_file"
done

# ─── Summary ──────────────────────────────────────────────────────────────────

summary
