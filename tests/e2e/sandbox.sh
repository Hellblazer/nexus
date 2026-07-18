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

# RDR-184 P0 guard-surface gap (nexus-ccs9v.4/.5 review): this script
# mutates the IDENTICAL fixed resource ($HOME/nexus-sandbox, unconditional
# rm -rf below) as release-sandbox.sh — SAME lockdir name as
# release-sandbox.sh, not a new lock, because it is the same underlying
# resource: running this while release-sandbox.sh holds its lock (e.g.
# mid smoke/shakedown/tmux run) would otherwise rm -rf the sandbox HOME
# out from under it with zero contention signal. Acquired here, before
# the first mutation, same pattern as the other guarded harnesses.
# shellcheck source=./lib/lock.sh disable=SC1091
source "$SCRIPT_DIR/lib/lock.sh"
LOCKDIR="/tmp/nexus-e2e-locks/release-sandbox.lock"
mkdir -p "$(dirname "$LOCKDIR")"
# Held-by-parent seam: release-sandbox.sh already holds this exact lock when
# it invokes us on its fresh-sandbox path — the lock is non-reentrant, so a
# second acquire here self-deadlocks (found cutting 6.13.0: the fresh path
# had never run under the hardened lock). The parent sets the env ONLY when
# it holds the lock; standalone invocations still acquire + release.
if [[ -z "${NX_E2E_LOCK_HELD_BY_PARENT:-}" ]]; then
    lock_acquire "$LOCKDIR" || exit 1
    trap 'lock_release "$LOCKDIR" 2>/dev/null || true' EXIT
    echo "[rdr-184] lock acquired: $LOCKDIR (pid $$)" >&2
else
    echo "[rdr-184] lock held by parent (pid $PPID) — not re-acquiring" >&2
fi
# Test seam (RDR-184 P0.2/.4, nexus-ccs9v.2/.4): tests/e2e/lib/harness_lock_test.sh
# sets this to prove a concurrent invocation gets PAST the lock without ever
# running this harness's real body (rm -rf $SANDBOX etc). No-op in normal use.
[[ -n "${NX_E2E_LOCK_SELFTEST:-}" ]] && exit 0

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
