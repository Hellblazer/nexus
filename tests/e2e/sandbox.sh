#!/usr/bin/env bash
# sandbox.sh — create a bare isolated Claude HOME for manual testing.
# Does NOT install anything. Source the activate file to enter the sandbox.
#
# Usage:
#   ./tests/e2e/sandbox.sh
#   source ~/nexus-sandbox/activate

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SANDBOX="$HOME/nexus-sandbox"

# Load API keys
[[ -f "$REPO_ROOT/.env" ]] && set -a && source "$REPO_ROOT/.env" && set +a

# ANTHROPIC_API_KEY is optional: needed only when the sandbox will spawn Claude
# Code (interactive / tmux modes). Pure CLI smoke (release-sandbox.sh smoke) and
# the `shell` mode that just exercises `nx` do not need it.
: "${ANTHROPIC_API_KEY:=}"

# Bare Claude home
rm -rf "$SANDBOX"
mkdir -p "$SANDBOX/.claude/plugins"
echo '{"hasCompletedOnboarding":true}' > "$SANDBOX/.claude.json"

# Activate script
cat > "$SANDBOX/activate" <<EOF
export SANDBOX_ORIG_HOME="\$HOME"
export HOME="$SANDBOX"
export PATH="$SANDBOX/.local/bin:\$PATH"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
export VOYAGE_API_KEY="${VOYAGE_API_KEY:-}"
export CHROMA_API_KEY="${CHROMA_API_KEY:-}"
export CHROMA_TENANT="${CHROMA_TENANT:-}"
export CHROMA_DATABASE="${CHROMA_DATABASE:-default_database}"
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
echo "sandbox active — HOME=\$HOME"
echo "deactivate: export HOME=\$SANDBOX_ORIG_HOME"
EOF
chmod 600 "$SANDBOX/activate"

echo "Sandbox ready. Enter with:"
echo "  source ~/nexus-sandbox/activate"
