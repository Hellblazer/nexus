#!/usr/bin/env bash
# Authenticate Claude Code for e2e tests.
#
# Strategy (tries in order):
#   1. Keychain extract (macOS): reads "Claude Code-credentials" from macOS
#      Keychain and writes it to tests/e2e/.claude-auth/.credentials.json.
#      Fast, no browser needed — may prompt for Keychain password/Touch ID.
#   2. Interactive fallback: runs Claude Code interactively in Docker so you
#      can complete the OAuth flow yourself, then /exit.
#
# Credentials are saved to tests/e2e/.claude-auth/ and reused by run.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
AUTH_DIR="$REPO_ROOT/tests/e2e/.claude-auth"

mkdir -p "$AUTH_DIR"

# If already authenticated, check token expiry before short-circuiting.
# Stale cached creds (expiresAt in the past) silently break the harness
# with "Failed to authenticate. API Error: 401" inside tmux — the fixed
# cache never refreshes so every subsequent run is broken until someone
# manually deletes .credentials.json. The keychain holds the fresh token;
# we just need to notice when the cache is stale and re-extract.
if [[ -f "$AUTH_DIR/.credentials.json" ]]; then
    _now_ms=$(( $(date +%s) * 1000 ))
    _exp_ms=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    print(int(d.get('claudeAiOauth', {}).get('expiresAt', 0)))
except Exception:
    print(0)
" "$AUTH_DIR/.credentials.json" 2>/dev/null || echo 0)
    if [[ "$_exp_ms" -gt "$_now_ms" ]]; then
        _remaining_s=$(( (_exp_ms - _now_ms) / 1000 ))
        echo "Already authenticated (cached creds valid for ~$(( _remaining_s / 3600 ))h)"
        echo "Delete tests/e2e/.claude-auth/.credentials.json and re-run to force refresh."
        exit 0
    fi
    echo "Cached credentials are stale (expiresAt already past) — refreshing from Keychain…"
    rm -f "$AUTH_DIR/.credentials.json"
fi

# ─── Strategy 1: macOS Keychain ───────────────────────────────────────────────
# Claude Code stores OAuth credentials in the macOS Keychain under the service
# name "Claude Code-credentials".  Extract them directly — no browser needed.

if [[ "$(uname)" == "Darwin" ]] && command -v security &>/dev/null; then
    echo "Trying macOS Keychain extraction..."
    creds=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null || true)
    if [[ -n "$creds" ]]; then
        echo "$creds" > "$AUTH_DIR/.credentials.json"

        # Also extract the oauthAccount from ~/.claude.json.
        # Claude Code uses oauthAccount to recognize the user as logged in —
        # without it, it shows the login selector even when .credentials.json
        # contains valid tokens.
        home_cfg="$HOME/.claude.json"
        if [[ -f "$home_cfg" ]]; then
            python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
minimal = {'hasCompletedOnboarding': True, 'oauthAccount': d.get('oauthAccount', {})}
with open(sys.argv[2], 'w') as f:
    json.dump(minimal, f)
print('  oauthAccount saved to tests/e2e/.claude-auth/claude.json')
" "$home_cfg" "$AUTH_DIR/claude.json"
        fi

        echo ""
        echo "✓ Credentials extracted from macOS Keychain"
        echo "  Saved to tests/e2e/.claude-auth/.credentials.json"
        echo "  Run: ./tests/e2e/run.sh"
        exit 0
    else
        echo "  Keychain entry not found or access denied — falling back to interactive."
    fi
fi

# ─── Strategy 2: Interactive Docker session ───────────────────────────────────

IMAGE="nexus-e2e-img"
if ! docker image inspect "$IMAGE" &>/dev/null; then
    echo "Building $IMAGE first..."
    docker build -f "$REPO_ROOT/.devcontainer/Dockerfile" -t "$IMAGE" "$REPO_ROOT"
fi

echo ""
echo "Starting Claude Code for one-time interactive authentication..."
echo "  1. At the login prompt, choose option 2 (Claude.ai / browser)"
echo "  2. Open the URL in your browser and complete the login"
echo "  3. Copy the code shown on the page and paste it here when prompted"
echo "  4. When Claude's prompt appears, type:  /exit"
echo ""

# Run interactively in a proper shell (not bash -c) so TTY is fully allocated.
# The user types 'claude --dangerously-skip-permissions' themselves, giving
# a better interactive terminal experience for pasting the OAuth code.
docker run -it --rm \
    --name nexus-e2e-auth \
    -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
    -v "$AUTH_DIR":/home/node/.claude \
    -v "$REPO_ROOT":/workspace \
    -w /workspace \
    "$IMAGE" \
    bash --login -c 'echo "Run: claude --dangerously-skip-permissions"; echo "Then /exit when done."; exec bash --login'

if [[ -f "$AUTH_DIR/.credentials.json" ]]; then
    echo ""
    echo "✓ Credentials saved to tests/e2e/.claude-auth/.credentials.json"
    echo "  Run: ./tests/e2e/run.sh"
else
    echo ""
    echo "✗ No credentials found — did you complete login and /exit?"
    echo "  Tip: credentials are saved to ~/.claude/.credentials.json inside the container"
fi
