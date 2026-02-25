#!/usr/bin/env bash
# E2E test helpers — docker+tmux based Claude Code interaction

CONTAINER="${CONTAINER:-nexus-e2e}"
TMUX_SESSION="e2e"
PASS=0
FAIL=0
_SCENARIO=""

# ─── Docker/tmux primitives ──────────────────────────────────────────────────

# Run a command inside the container
cexec() { docker exec "$CONTAINER" "$@"; }

# Run a shell command inside the container and return output
crun() { docker exec "$CONTAINER" bash -c "$1"; }

# Send keys to the Claude tmux pane
send_keys() {
    local text="$1" key="${2:-Enter}"
    cexec tmux send-keys -t "${TMUX_SESSION}:0.0" "$text" "$key"
}

# Capture last N lines of pane output
capture() {
    cexec tmux capture-pane -t "${TMUX_SESSION}:0.0" -p -S "${1:--150}"
}

# ─── Timing helpers ──────────────────────────────────────────────────────────

# Poll until pane output matches pattern, or timeout
poll_for() {
    local pattern="$1" timeout="${2:-120}" label="${3:-$pattern}"
    for i in $(seq 1 $((timeout / 2))); do
        sleep 2
        if capture | grep -qE "$pattern"; then
            return 0
        fi
    done
    echo "    TIMEOUT waiting for: $label (${timeout}s)"
    return 1
}

# Poll until pane output does NOT match pattern (i.e. activity stopped)
poll_until_gone() {
    local pattern="$1" timeout="${2:-60}"
    for i in $(seq 1 $((timeout / 2))); do
        sleep 2
        if ! capture | grep -qE "$pattern"; then
            return 0
        fi
    done
    return 1
}

# ─── Claude session management ───────────────────────────────────────────────

# Start a fresh Claude session in the tmux pane.
# Handles the splash-screen-swallows-Enter gotcha:
#   1. Launch claude
#   2. Wait for splash to clear
#   3. Type prompt (no Enter yet)
#   4. Send bare Enter
claude_start() {
    send_keys "claude --dangerously-skip-permissions" Enter
    sleep 6  # wait for splash to clear
}

# Send a prompt to an already-running Claude session.
# Must be called AFTER claude_start has finished (splash gone).
claude_prompt() {
    local prompt="$1"
    send_keys "$prompt" ""   # type text, no Enter
    sleep 0.3
    send_keys "" Enter       # bare Enter — now it goes to Claude
}

# Exit Claude cleanly
claude_exit() {
    send_keys "" C-c         # cancel any in-progress response
    sleep 0.3
    send_keys "/exit" Enter
    sleep 2
}

# Wait for Claude to finish responding (not actively generating)
claude_wait() {
    local timeout="${1:-120}"
    # Wait until "Simmering/Running/Cerebrating" disappears from pane
    poll_until_gone "Simmering…|Running…|Cerebrating…|esc to interrupt" "$timeout" || true
    sleep 1  # extra settle time
}

# ─── Assertions ──────────────────────────────────────────────────────────────

pass() { echo "    ✓ $1"; PASS=$(( PASS + 1 )); }
fail() { echo "    ✗ $1"; FAIL=$(( FAIL + 1 )); }

assert_output() {
    local label="$1" pattern="$2"
    if capture | grep -qE "$pattern"; then
        pass "$label"
    else
        fail "$label — pattern not found: $pattern"
        echo "    --- last pane output ---"
        capture -30 | sed 's/^/    | /'
        echo "    ---"
    fi
}

assert_cmd() {
    local label="$1" cmd="$2" pattern="$3"
    local out
    out=$(crun "$cmd" 2>&1) || true
    if echo "$out" | grep -qE "$pattern"; then
        pass "$label"
    else
        fail "$label — expected '$pattern' in: $out"
    fi
}

# ─── Scenario runner ─────────────────────────────────────────────────────────

scenario() {
    _SCENARIO="$1"
    echo ""
    echo "┌─ Scenario: $_SCENARIO"
}

scenario_end() {
    echo "└─ Done: $_SCENARIO"
}

# ─── Summary ─────────────────────────────────────────────────────────────────

summary() {
    echo ""
    echo "══════════════════════════════════"
    echo " Results: ${PASS} passed, ${FAIL} failed"
    echo "══════════════════════════════════"
    [[ $FAIL -eq 0 ]]
}
