#!/usr/bin/env bash
# sandbox.sh — set up an isolated Claude + nx sandbox for manual rc testing.
#
# Usage:
#   ./tests/e2e/sandbox.sh                  # install from PyPI rc3
#   ./tests/e2e/sandbox.sh --source         # install from local repo source
#
# After running, follow the printed instructions to enter the sandbox shell.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SANDBOX="$HOME/nexus-sandbox"
MODE="pypi"  # or "source"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) MODE="source"; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Load API keys from .env if present
if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a; source "$REPO_ROOT/.env"; set +a
fi

: "${ANTHROPIC_API_KEY:?'ANTHROPIC_API_KEY must be set (in .env or environment)'}"

# ── Clean slate ────────────────────────────────────────────────────────────────

echo "Setting up sandbox at $SANDBOX ..."
rm -rf "$SANDBOX"
mkdir -p "$SANDBOX/.claude/plugins"

# ── Claude auth ────────────────────────────────────────────────────────────────

AUTH_DIR="$SCRIPT_DIR/.claude-auth"
if [[ -f "$AUTH_DIR/.credentials.json" ]]; then
    cp "$AUTH_DIR/.credentials.json" "$SANDBOX/.claude/.credentials.json"
    [[ -f "$AUTH_DIR/claude.json" ]] && cp "$AUTH_DIR/claude.json" "$SANDBOX/.claude.json"
    echo "Claude OAuth credentials injected."
else
    echo '{"hasCompletedOnboarding":true}' > "$SANDBOX/.claude.json"
    echo "Using API key auth (no OAuth credentials found)."
fi

# ── Locate superpowers ─────────────────────────────────────────────────────────

SUPERPOWERS_CACHE="$HOME/.claude/plugins/cache/claude-plugins-official/superpowers"
SUPERPOWERS_DIR=""
if [[ -d "$SUPERPOWERS_CACHE" ]]; then
    SUPERPOWERS_DIR=$(find "$SUPERPOWERS_CACHE" -maxdepth 1 -mindepth 1 -type d \
        | sort -V | tail -1)
fi

SUPERPOWERS_ENTRY=""
SUPERPOWERS_ENABLED=""
if [[ -n "$SUPERPOWERS_DIR" ]]; then
    SUPERPOWERS_VERSION="$(basename "$SUPERPOWERS_DIR")"
    echo "superpowers found: v$SUPERPOWERS_VERSION"
    SUPERPOWERS_ENTRY=",
    \"superpowers@claude-plugins-official\": [
      {
        \"scope\": \"user\",
        \"installPath\": \"$SUPERPOWERS_DIR\",
        \"version\": \"$SUPERPOWERS_VERSION\",
        \"installedAt\": \"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\",
        \"lastUpdated\": \"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\"
      }
    ]"
    SUPERPOWERS_ENABLED=',
    "superpowers@claude-plugins-official": true'
else
    echo "WARNING: superpowers not found — nx skills referencing it won't be exercised."
fi

# ── Clone or use nx plugin ─────────────────────────────────────────────────────

if [[ "$MODE" == "source" ]]; then
    NX_PATH="$REPO_ROOT/nx"
    NX_VERSION="dev"
    echo "Using nx plugin from local source: $NX_PATH"
else
    NX_CLONE="$SANDBOX/nexus-rc3"
    echo "Cloning nx plugin from v1.0.0rc3 tag..."
    git clone --branch v1.0.0rc3 --depth 1 \
        https://github.com/Hellblazer/nexus.git "$NX_CLONE" 2>&1 | tail -3
    NX_PATH="$NX_CLONE/nx"
    NX_VERSION="1.0.0rc3"
fi

# ── Register plugins ───────────────────────────────────────────────────────────

NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
cat > "$SANDBOX/.claude/plugins/installed_plugins.json" <<PLUGINS_EOF
{
  "version": 2,
  "plugins": {
    "nx@nexus-plugins": [
      {
        "scope": "user",
        "installPath": "$NX_PATH",
        "version": "$NX_VERSION",
        "installedAt": "$NOW",
        "lastUpdated": "$NOW"
      }
    ]${SUPERPOWERS_ENTRY}
  }
}
PLUGINS_EOF

cat > "$SANDBOX/.claude/settings.json" <<SETTINGS_EOF
{
  "enabledPlugins": {
    "nx@nexus-plugins": true${SUPERPOWERS_ENABLED}
  },
  "skipDangerousModePermissionPrompt": true
}
SETTINGS_EOF

# ── Install nx CLI ─────────────────────────────────────────────────────────────

REAL_UV="${HOME}/.local/bin/uv"
[[ ! -x "$REAL_UV" ]] && REAL_UV="$(command -v uv)"

if [[ "$MODE" == "source" ]]; then
    echo "Installing nx CLI from local source..."
    HOME="$SANDBOX" "$REAL_UV" tool install "$REPO_ROOT" --force --python 3.12 2>&1 | tail -3
else
    echo "Installing nx CLI from PyPI (conexus==1.0.0rc3)..."
    HOME="$SANDBOX" "$REAL_UV" tool install "conexus==1.0.0rc3" --force --python 3.12 2>&1 | tail -3
fi

echo "nx installed at $SANDBOX/.local/bin/nx"

# ── Write sandbox env file ─────────────────────────────────────────────────────

cat > "$SANDBOX/activate" <<EOF
# Source this to enter the nx sandbox:  source ~/nexus-sandbox/activate
export SANDBOX_ORIG_HOME="\$HOME"
export HOME="$SANDBOX"
export PATH="$SANDBOX/.local/bin:\$PATH"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}"
export VOYAGE_API_KEY="${VOYAGE_API_KEY:-}"
export CHROMA_API_KEY="${CHROMA_API_KEY:-}"
export CHROMA_TENANT="${CHROMA_TENANT:-}"
export CHROMA_DATABASE="${CHROMA_DATABASE:-default_database}"
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
echo "nx sandbox active (HOME=\$HOME)"
echo "  nx version: \$(nx --version 2>/dev/null || echo 'not found')"
echo "  Deactivate: export HOME=\$SANDBOX_ORIG_HOME"
EOF

chmod 600 "$SANDBOX/activate"

# ── Done ───────────────────────────────────────────────────────────────────────

echo ""
echo "══════════════════════════════════════"
echo " Sandbox ready: $SANDBOX"
echo "══════════════════════════════════════"
echo ""
echo "Enter the sandbox:"
echo ""
echo "  source ~/nexus-sandbox/activate"
echo ""
echo "Then use claude and nx normally."
echo "Teardown: rm -rf ~/nexus-sandbox"
