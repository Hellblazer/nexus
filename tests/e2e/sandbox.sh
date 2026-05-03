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

# Activate script. Built via printf rather than a here-doc because
# bash here-docs hang in some non-interactive contexts (Claude Code
# harness shells, certain CI runners) where the parent stdin is
# attached to a pipe that bash's here-doc machinery never closes.
# printf has no such dependency.
{
    printf '%s\n' 'export SANDBOX_ORIG_HOME="$HOME"'
    printf 'export HOME="%s"\n' "$SANDBOX"
    printf 'export PATH="%s/.local/bin:$PATH"\n' "$SANDBOX"
    printf 'export ANTHROPIC_API_KEY="%s"\n' "${ANTHROPIC_API_KEY:-}"
    printf 'export VOYAGE_API_KEY="%s"\n' "${VOYAGE_API_KEY:-}"
    printf 'export CHROMA_API_KEY="%s"\n' "${CHROMA_API_KEY:-}"
    printf 'export CHROMA_TENANT="%s"\n' "${CHROMA_TENANT:-}"
    printf 'export CHROMA_DATABASE="%s"\n' "${CHROMA_DATABASE:-default_database}"
    printf '%s\n' 'unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT'
    printf '%s\n' 'echo "sandbox active — HOME=$HOME"'
    printf '%s\n' 'echo "deactivate: export HOME=$SANDBOX_ORIG_HOME"'
} > "$SANDBOX/activate"
chmod 600 "$SANDBOX/activate"

echo "Sandbox ready. Enter with:"
echo "  source ~/nexus-sandbox/activate"
