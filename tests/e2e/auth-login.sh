#!/usr/bin/env bash
# Authenticate Claude Code for e2e tests.
#
# Strategy (tries in order):
#   1. Keychain extract (macOS): picks the freshest USABLE
#      "Claude Code-credentials" item from macOS Keychain — via the shared
#      tests/e2e/lib/claude_credentials.py picker, not a bare, unscoped
#      `security find-generic-password` (see below) — and writes it to
#      tests/e2e/.claude-auth/.credentials.json. Fast, no browser needed —
#      may prompt for Keychain password/Touch ID.
#   2. Interactive fallback: runs Claude Code interactively in Docker so you
#      can complete the OAuth flow yourself, then /exit.
#
# Credentials are saved to tests/e2e/.claude-auth/ and reused by run.sh.
#
# CRED_TOOL (nexus-galkv.19): more than one macOS Keychain item can carry
# the service name "Claude Code-credentials" — on this box an
# acct="unknown" item is an empty husk (accessToken "", refreshToken "",
# expiresAt 0) alongside the live acct=<login user> item the CLI actually
# refreshes. A bare `security find-generic-password -s ... -w` (no `-a`)
# returns an ARBITRARY match; on 2026-09-15 it returned the husk, this
# script wrote it over the only fallback snapshot, and interactive Claude
# Code showed "Not logged in" while `claude -p` failed "OAuth session
# expired and could not be refreshed". `claude_credentials.py pick`
# enumerates every account under the service and picks the freshest one
# that actually carries a token; `check FILE` asks the same "does this
# carry a usable token" question of an on-disk file. See
# tests/cc-validation/README.md § Auth for the fuller incident writeup
# (the original fix, for a sibling harness that never shared it here).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
AUTH_DIR="$REPO_ROOT/tests/e2e/.claude-auth"
CRED_TOOL="$REPO_ROOT/tests/e2e/lib/claude_credentials.py"

mkdir -p "$AUTH_DIR"

# If already authenticated, check USABILITY, not just an expiresAt vs. wall
# clock comparison — the old check here compared expiresAt > now, which
# misreads a token-less husk's expiresAt=0 as "already past" (0 <= now is
# always true) and so happened to re-extract on a husk, but for the wrong
# reason: a husk with a FUTURE (bogus) expiresAt would have short-circuited
# as "valid" instead of being rejected for carrying no token at all.
# `check` asks the real question via the same verdict `pick` uses.
if [[ -f "$AUTH_DIR/.credentials.json" ]] && python3 "$CRED_TOOL" check "$AUTH_DIR/.credentials.json" 2>/dev/null; then
    echo "Already authenticated (cached credential is usable)"
    echo "Delete tests/e2e/.claude-auth/.credentials.json and re-run to force refresh."
    exit 0
fi
if [[ -f "$AUTH_DIR/.credentials.json" ]]; then
    echo "Cached credentials are not usable — refreshing from Keychain…"
    rm -f "$AUTH_DIR/.credentials.json"
fi

# ─── Strategy 1: macOS Keychain ───────────────────────────────────────────────
# Claude Code stores OAuth credentials in the macOS Keychain under the service
# name "Claude Code-credentials". Extract the freshest USABLE one via the
# shared picker — never a bare `security find-generic-password` (see
# CRED_TOOL note above for why that silently selects an arbitrary, possibly
# token-less, item).

if [[ "$(uname)" == "Darwin" ]] && command -v security &>/dev/null; then
    echo "Trying macOS Keychain extraction..."
    if creds="$(python3 "$CRED_TOOL" pick)"; then
        # `pick` only ever returns a payload that already passed the same
        # verdict `check` applies — the write below is therefore always
        # from a credential that passed the usability check.
        printf '%s' "$creds" > "$AUTH_DIR/.credentials.json"

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
        echo "  No usable Keychain item found — falling back to interactive."
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
    echo "AUTH-LOGIN PASSED — credentials saved to tests/e2e/.claude-auth/.credentials.json"
    echo "  Run: ./tests/e2e/run.sh"
else
    # nexus-epj0b: this used to print the ✗ line and exit 0 — failure and
    # success were indistinguishable to any `auth-login.sh && ...` caller,
    # and the only signal was prose scrolling past a human. A setup helper
    # still owes callers a truthful exit code and a greppable sentinel.
    echo ""
    echo "AUTH-LOGIN FAILED — no credentials found. Did you complete login and /exit?"
    echo "  Tip: credentials are saved to ~/.claude/.credentials.json inside the container"
    echo "  Faster alternative: run 'claude /login' in a normal session on this host, then re-run this script."
    exit 1
fi
