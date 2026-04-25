#!/usr/bin/env bash
# release-sandbox.sh — high-fidelity local pre-merge verification.
#
# Combines:
#   - scripts/reinstall-tool.sh   (wheel-equivalent install via uv tool)
#   - tests/e2e/sandbox.sh        (isolated $HOME for Claude Code state)
#   - tests/cc-validation/lib.sh  (tmux primitives, used by tmux mode)
#
# Why this exists: merging to main to "test things out" is dangerous because
# the wheel-install path (uv tool install) resolves package data and version-
# gated migrations differently from the editable install that pytest uses.
# Run this BEFORE pushing/merging anything that touches: install/packaging,
# T2 migrations, MCP servers, hooks, plugin manifests, commands that read
# T2/T3 state, or anything tagged "ships to users".
#
# Modes:
#   smoke    — install + activate + post-install canary checks. ~2 min.
#   shell    — install + activate + drop into a subshell with sandbox env.
#              Exit the subshell to tear down (HOME restored automatically).
#   tmux     — install + activate + launch Claude Code in tmux against
#              the sandbox. Useful for exercising MCP / hooks / skills.
#   reset    — tear down ~/nexus-sandbox without reinstalling.
#
# Source-of-truth doc: tests/e2e/release-sandbox.md
# Companion gist: https://gist.github.com/Hellblazer/511a05e1bf79dd6ea20be962d0ca04af

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SANDBOX="$HOME/nexus-sandbox"
TMUX_SESSION="${TMUX_SESSION:-nexus-sandbox}"

MODE="${1:-help}"
shift || true

_die() { echo "ERROR: $*" >&2; exit 1; }

_print_help() {
    cat <<EOF
Usage: $0 <mode> [options]

Modes:
  smoke      Reinstall + activate + run nx upgrade --dry-run + nx doctor checks.
             Verifies the wheel install + migrations + health surface.
  shell      Reinstall + activate + drop into a subshell with HOME=\$SANDBOX.
             Use this for manual nx index, nx search, etc. Exit normally to
             tear down.
  tmux       Reinstall + activate + launch Claude Code interactively in tmux.
             Useful for end-to-end exercises against MCP / plugin / hooks.
             Requires tests/e2e/.claude-auth/.credentials.json (run
             tests/e2e/auth-login.sh first).
  reset      Remove ~/nexus-sandbox. Does NOT reinstall.
  help       Print this message.

Common options (post-mode):
  --skip-install   Skip the reinstall step. Useful when the tool venv is
                   already at the version you want to exercise.
  --keep-existing  Reuse \$HOME/nexus-sandbox if it exists (default: blow away
                   and recreate so state is reproducible).

Examples:
  # Pre-merge smoke after a refactor
  $0 smoke

  # Hand-test indexing into the sandbox
  $0 shell
  (sandbox) nx index repo /path/to/test-repo
  (sandbox) nx taxonomy status
  (sandbox) exit

  # Spin up Claude Code against the sandbox
  $0 tmux

  # Skip reinstall (e.g. iterating on shell flow)
  $0 shell --skip-install

EOF
}

# ── Option parsing ───────────────────────────────────────────────────────────

SKIP_INSTALL=0
KEEP_EXISTING=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-install) SKIP_INSTALL=1; shift ;;
        --keep-existing) KEEP_EXISTING=1; shift ;;
        --help|-h) _print_help; exit 0 ;;
        *) _die "unknown option: $1 (use $0 help)" ;;
    esac
done

# ── Mode dispatch ────────────────────────────────────────────────────────────

if [[ "$MODE" == "help" || "$MODE" == "--help" || "$MODE" == "-h" ]]; then
    _print_help
    exit 0
fi

if [[ "$MODE" == "reset" ]]; then
    if [[ -d "$SANDBOX" ]]; then
        echo "Removing $SANDBOX ..."
        rm -rf "$SANDBOX"
        echo "Sandbox removed."
    else
        echo "No sandbox at $SANDBOX — nothing to reset."
    fi
    exit 0
fi

if [[ "$MODE" != "smoke" && "$MODE" != "shell" && "$MODE" != "tmux" ]]; then
    _die "unknown mode: $MODE (use $0 help)"
fi

# ── Step 1 — reinstall (unless skipped) ──────────────────────────────────────

if (( SKIP_INSTALL == 0 )); then
    echo "[1/3] Reinstalling nx CLI from $REPO_ROOT ..."
    (cd "$REPO_ROOT" && uv sync >/dev/null 2>&1)
    "$REPO_ROOT/scripts/reinstall-tool.sh" >/dev/null
    echo "      $(nx --version 2>/dev/null || echo 'nx --version failed')"
else
    echo "[1/3] Skipping reinstall (--skip-install). nx version: $(nx --version 2>/dev/null || echo 'unknown')"
fi

# ── Step 2 — create sandbox HOME ─────────────────────────────────────────────

if [[ -d "$SANDBOX" && $KEEP_EXISTING -eq 0 ]]; then
    echo "[2/3] Recreating sandbox at $SANDBOX (use --keep-existing to reuse)"
    rm -rf "$SANDBOX"
elif [[ -d "$SANDBOX" ]]; then
    echo "[2/3] Reusing existing sandbox at $SANDBOX"
fi

if [[ ! -d "$SANDBOX" ]]; then
    echo "[2/3] Creating fresh sandbox at $SANDBOX ..."
    "$REPO_ROOT/tests/e2e/sandbox.sh" >/dev/null
fi

# ── Step 3 — execute mode ────────────────────────────────────────────────────

# shellcheck source=/dev/null
. "$SANDBOX/activate"

case "$MODE" in
    smoke)
        echo "[3/3] Smoke checks (running from /tmp to catch package-data bugs):"
        cd /tmp
        echo "  nx --version: $(nx --version)"
        echo
        echo "  nx upgrade --dry-run:"
        nx upgrade --dry-run 2>&1 | sed 's/^/    /' || true
        echo
        echo "  nx upgrade (apply):"
        nx upgrade 2>&1 | sed 's/^/    /' || true
        echo
        for check in --check-schema --check-plan-library --check-taxonomy --check-hooks; do
            echo "  nx doctor $check:"
            if nx doctor "$check" 2>&1 | sed 's/^/    /'; then
                echo "    [pass]"
            else
                echo "    [FAIL] -- exit non-zero" >&2
            fi
            echo
        done
        echo "[done] Sandbox state at $SANDBOX. Run '$0 reset' to tear down."
        ;;

    shell)
        echo "[3/3] Dropping into subshell with HOME=$SANDBOX ..."
        echo "      Exit the subshell to restore your real \$HOME."
        echo
        # Subshell: env stays sandboxed, exit returns control + restores HOME.
        cd "$SANDBOX"
        exec env \
            HOME="$SANDBOX" \
            PATH="$SANDBOX/.local/bin:$PATH" \
            VOYAGE_API_KEY="${VOYAGE_API_KEY:-}" \
            CHROMA_API_KEY="${CHROMA_API_KEY:-}" \
            CHROMA_TENANT="${CHROMA_TENANT:-}" \
            CHROMA_DATABASE="${CHROMA_DATABASE:-default_database}" \
            ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
            NEXUS_SANDBOX=1 \
            PS1="(sandbox) $ " \
            bash --noprofile --norc -i
        ;;

    tmux)
        if ! command -v tmux >/dev/null 2>&1; then
            _die "tmux not installed (brew install tmux)"
        fi
        AUTH_DIR="$REPO_ROOT/tests/e2e/.claude-auth"
        if [[ ! -f "$AUTH_DIR/.credentials.json" ]]; then
            _die "missing $AUTH_DIR/.credentials.json — run tests/e2e/auth-login.sh first"
        fi
        # Reuse cc-validation lib for tmux primitives + claude_start.
        export TEST_HOME="$SANDBOX"
        export TMUX_SESSION
        echo "[3/3] Launching Claude Code in tmux session '$TMUX_SESSION' ..."
        echo "      Detach: Ctrl-b d   |   Kill: tmux kill-session -t $TMUX_SESSION"
        echo
        # shellcheck source=/dev/null
        . "$REPO_ROOT/tests/e2e/lib.sh"
        # Ensure auth credentials are reachable inside the sandbox HOME.
        mkdir -p "$SANDBOX/.claude"
        cp "$AUTH_DIR/.credentials.json" "$SANDBOX/.claude/.credentials.json"
        if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
            tmux kill-session -t "$TMUX_SESSION"
        fi
        tmux new-session -d -s "$TMUX_SESSION" -x 220 -y 50 \
            "env HOME='$SANDBOX' PATH='$SANDBOX/.local/bin:$PATH' bash -i"
        sleep 1
        tmux send-keys -t "$TMUX_SESSION" "claude" Enter
        echo "Attaching ... (Ctrl-b d to detach without killing)"
        tmux attach -t "$TMUX_SESSION"
        ;;
esac
