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
#   smoke     — install + activate + post-install canary checks. ~2 min.
#   shakedown — full ensemble: smoke + index repo/pdf/rdr + search/query/T1/T2 +
#               link graph readback + T1 turd sniff. ~5–10 min.
#   shell     — install + activate + drop into a subshell with sandbox env.
#               Exit the subshell to tear down (HOME restored automatically).
#   tmux      — install + activate + launch Claude Code in tmux against
#               the sandbox. Useful for exercising MCP / hooks / skills.
#   reset     — tear down ~/nexus-sandbox without reinstalling.
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
             Verifies the wheel install + migrations + health surface. ~2 min.
  shakedown  Full ensemble: smoke + nx index repo/pdf/rdr + cross-corpus search
             + T2 memory roundtrip + T1 scratch use + catalog link readback +
             T1 turd sniff. Exercises every pipeline against a fresh install.
             ~5–10 min. Uses tests/fixtures/tc-sql.pdf as the PDF probe.
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

if [[ "$MODE" != "smoke" && "$MODE" != "shakedown" && "$MODE" != "shell" && "$MODE" != "tmux" ]]; then
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
        # Force local mode so the smoke does not contact ChromaDB Cloud
        # even if the parent shell has CHROMA_* set. The sandbox HOME is
        # empty by design — there is no cloud data to populate from. Same
        # pattern shakedown uses below; tests/e2e/run.sh has the long
        # explanation.
        export NX_LOCAL=1
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE
        echo "[3/3] Smoke checks (running from /tmp, NX_LOCAL=1):"
        cd /tmp
        echo "  nx --version: $(nx --version)"
        echo
        echo "  nx upgrade --dry-run:"
        nx upgrade --dry-run 2>&1 | sed 's/^/    /' || true
        echo
        echo "  nx upgrade (apply):"
        nx upgrade 2>&1 | sed 's/^/    /' || true
        echo
        # nx catalog setup seeds 12 builtin plan templates that
        # --check-plan-library verifies. Without this step the doctor
        # check fails on every fresh sandbox — that is "you forgot the
        # second setup step", not "something is genuinely broken." Make
        # smoke green-green-green when the install is healthy.
        echo "  nx catalog setup (seeds plan library + initializes catalog):"
        nx catalog setup 2>&1 | tail -5 | sed 's/^/    /' || true
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

    shakedown)
        # Ensemble pipeline check: every nx surface exercised in sequence
        # against the wheel install. Uses the smaller PDF fixture (tc-sql)
        # for speed. T1 sniff at the start + end catches lifecycle bugs
        # (orphan tmpdirs, leaked session files).
        #
        # Force local mode so the shakedown does not contact ChromaDB Cloud
        # even if the parent shell has CHROMA_* set. Mirrors tests/e2e/run.sh.
        export NX_LOCAL=1
        unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE
        echo "[3/3] Shakedown: full pipeline ensemble (running from /tmp, NX_LOCAL=1)"
        cd /tmp

        echo
        echo "── T1 sniff: BEFORE ──"
        T1_DIR_PARENT="${TMPDIR%/}"; [[ -z "$T1_DIR_PARENT" ]] && T1_DIR_PARENT=/tmp
        BEFORE_SESSIONS=$( { ls "$HOME/.config/nexus/sessions/" 2>/dev/null || true; } | wc -l | tr -d ' ')
        BEFORE_TMPDIRS=$( { ls -d "$T1_DIR_PARENT"/nx_t1_* 2>/dev/null || true; } | wc -l | tr -d ' ')
        echo "  session files: $BEFORE_SESSIONS  | tmpdirs: $BEFORE_TMPDIRS"

        echo
        echo "── nx --version + upgrade ──"
        nx --version | sed 's/^/  /'
        nx upgrade 2>&1 | sed 's/^/  /' || true

        echo
        echo "── 1/9 nx catalog setup (seeds plan library) ──"
        nx catalog setup 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 2/9 nx index repo ($REPO_ROOT) ──"
        nx index repo "$REPO_ROOT" 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 3/9 nx index pdf (tests/fixtures/tc-sql.pdf) ──"
        nx index pdf "$REPO_ROOT/tests/fixtures/tc-sql.pdf" \
            --collection knowledge__shakedown 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 4/9 nx index rdr ──"
        nx index rdr "$REPO_ROOT" 2>&1 | tail -5 | sed 's/^/  /' || true

        echo
        echo "── 5/9 cross-corpus search ──"
        nx search "catalog link graph" -m 3 2>&1 | tail -10 | sed 's/^/  /' || true

        echo
        echo "── 6/9 T2 memory roundtrip ──"
        SHAKE_TS=$(date +%s)
        nx memory put "shakedown marker $SHAKE_TS" \
            --project nexus_shakedown --title shakedown.md 2>&1 | tail -2 | sed 's/^/  /' || true
        nx memory get --project nexus_shakedown --title shakedown.md 2>&1 \
            | head -3 | sed 's/^/  /' || true

        echo
        echo "── 7/9 T1 scratch use (write + readback) ──"
        # Note: outside a Claude Code session, no SessionStart hook fires to
        # spawn the per-session ChromaDB HTTP server, so each `nx scratch *`
        # invocation falls back to its own EphemeralClient. Cross-invocation
        # readback is only possible inside a real Claude Code session. This
        # shakedown verifies put returns a doc id; cross-process visibility
        # is tested separately by the cc-validation harness.
        SCRATCH_OUT=$(nx scratch put "shakedown probe $SHAKE_TS" --tags=shakedown 2>&1 | tail -1)
        if echo "$SCRATCH_OUT" | grep -qE "Stored:"; then
            echo "  put: ok ($SCRATCH_OUT)"
        else
            echo "  put: [WARN] unexpected output — $SCRATCH_OUT"
        fi
        echo "  note: cross-invocation readback only works inside a Claude Code session"

        echo
        echo "── 8/9 catalog stats (registry + link graph readback) ──"
        nx catalog stats 2>&1 | head -15 | sed 's/^/  /' || true

        echo
        echo "── 9/9 nx doctor (all checks, post-activity) ──"
        for check in --check-schema --check-plan-library --check-taxonomy \
                     --check-hooks --check-tmpdirs; do
            echo "  $check:"
            nx doctor "$check" 2>&1 | tail -5 | sed 's/^/    /' || true
        done

        echo
        echo "── T1 sniff: AFTER ──"
        AFTER_SESSIONS=$( { ls "$HOME/.config/nexus/sessions/" 2>/dev/null || true; } | wc -l | tr -d ' ')
        AFTER_TMPDIRS=$( { ls -d "$T1_DIR_PARENT"/nx_t1_* 2>/dev/null || true; } | wc -l | tr -d ' ')
        echo "  session files: $AFTER_SESSIONS (was $BEFORE_SESSIONS)"
        echo "  tmpdirs:       $AFTER_TMPDIRS (was $BEFORE_TMPDIRS)"
        DELTA_S=$((AFTER_SESSIONS - BEFORE_SESSIONS))
        DELTA_T=$((AFTER_TMPDIRS - BEFORE_TMPDIRS))
        echo "  delta:         sessions+$DELTA_S  tmpdirs+$DELTA_T"
        if (( DELTA_S > 2 || DELTA_T > 2 )); then
            echo "  [WARN] T1 turd risk: net delta exceeds expected steady-state"
            echo "         Investigate $HOME/.config/nexus/sessions/ and $T1_DIR_PARENT/nx_t1_*"
        else
            echo "  [ok] T1 lifecycle within expected bounds"
        fi

        echo
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
