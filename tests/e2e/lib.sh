#!/usr/bin/env bash
# E2E test helpers — local tmux-based Claude Code interaction

TMUX_SESSION="e2e"
PASS=0
FAIL=0
SKIP=0
_SCENARIO=""

# ─── Local tmux primitives ───────────────────────────────────────────────────
# Tests run Claude Code locally in an isolated HOME ($TEST_HOME).
# No Docker — all commands execute on the local machine.

# Run a command directly (kept for compatibility with scenario scripts)
cexec() { "$@"; }

# Run a shell command in the isolated test environment
crun() {
    HOME="${TEST_HOME:-$HOME}" \
    PATH="${TEST_HOME:-$HOME}/.local/bin:$PATH" \
    bash -c "$1"
}

# Send keys to the Claude tmux pane.
# Usage: send_keys "text" [key]
#   send_keys "hello" Enter  → type "hello" then press Enter
#   send_keys "hello" ""     → type "hello" only (no key)
#   send_keys "" Enter       → press Enter only
send_keys() {
    local text="$1" key="${2:-Enter}"
    if [[ -n "$text" && -n "$key" ]]; then
        tmux send-keys -t "${TMUX_SESSION}" "$text" "$key"
    elif [[ -n "$text" ]]; then
        tmux send-keys -t "${TMUX_SESSION}" "$text"
    elif [[ -n "$key" ]]; then
        tmux send-keys -t "${TMUX_SESSION}" "$key"
    fi
}

# Capture last N lines of pane output
capture() {
    tmux capture-pane -t "${TMUX_SESSION}" -p -S "${1:--150}"
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
# HOME is already set to TEST_HOME in the pane (sourced from .env.test).
# Handles startup screens in sequence:
#   1. Workspace trust (if workspace not previously trusted) → Enter
#   2. Bypass permissions confirmation → Down + Enter
#   3. Login selector (if credentials missing) → Down + Enter for option 2
claude_start() {
    send_keys "claude --dangerously-skip-permissions" Enter

    # Give Claude time to initialize before checking screens.
    sleep 8

    # Poll for startup screens for up to 60 seconds.
    # Screens appear in sequence; each is handled once as it shows up.
    # Screens seen so far (in order, not all always appear):
    #   1. Workspace trust:  ❯ 1. Yes, I trust this folder  2. No, exit  → Enter
    #   2. Custom API key:   ❯ No (recommended) / Yes (use API key)      → Enter
    #   3. Bypass perms:     ❯ 1. No, exit  2. Yes, I accept             → Down+Enter
    #     NOTE: bypassed by skipDangerousModePermissionPrompt=true in settings.json
    #   4. Login selector:   ❯ 1. Claude account  2. Console (API key)   → Down+Enter
    #   5. OAuth fallback:   rare — restart Claude if seen
    # Break condition: status bar shows "bypass permissions on" (main prompt ready).

    local deadline=$(( $(date +%s) + 60 ))
    local _trust_done=0 _bypass_done=0 _login_done=0

    while [[ $(date +%s) -lt $deadline ]]; do
        local pane
        pane=$(capture)

        if [[ $_trust_done -eq 0 ]] && echo "$pane" | grep -qiE "trust this folder|project you trust"; then
            echo "    [auth] Workspace trust — accepting..."
            tmux send-keys -t "${TMUX_SESSION}" Enter
            _trust_done=1
            sleep 2

        elif [[ $_bypass_done -eq 0 ]] && echo "$pane" | grep -qiE "Bypass Permissions|Yes, I accept"; then
            echo "    [auth] Bypass permissions — accepting..."
            # Use direct tmux send-keys (not the send_keys wrapper) to avoid
            # any issue with empty-string first arg swallowing the arrow key.
            tmux send-keys -t "${TMUX_SESSION}" Down
            sleep 0.5
            tmux send-keys -t "${TMUX_SESSION}" Enter
            _bypass_done=1
            sleep 5

        elif echo "$pane" | grep -qiE "custom API key|Do you want to use this API key"; then
            echo "    [auth] Custom API key prompt — keeping OAuth session (No)..."
            # Default selection is already "No (recommended)" — just Enter
            tmux send-keys -t "${TMUX_SESSION}" Enter
            sleep 5  # long pause: screen transition takes a moment after Enter

        elif [[ $_login_done -eq 0 ]] && echo "$pane" | grep -qiE "Select login|login method|How would you like"; then
            echo "    [auth] Login selector — selecting option 2 (API key)..."
            tmux send-keys -t "${TMUX_SESSION}" Down
            sleep 0.5
            tmux send-keys -t "${TMUX_SESSION}" Enter
            _login_done=1
            sleep 6

        elif echo "$pane" | grep -qiE "oauth/authorize|Paste code"; then
            echo "    [auth] OAuth URL detected — credentials may be invalid."
            send_keys "" C-c
            sleep 1
            break

        elif echo "$pane" | grep -qiE "bypass permissions on|Type a message"; then
            # "bypass permissions on" only appears in the status bar at the main
            # prompt when --dangerously-skip-permissions is active.
            # This is more specific than "❯" which also appears in auth menus.
            break
        fi

        sleep 1
    done

    sleep 5  # extra settle time — plugin loading can take a moment
}

# Send a prompt to an already-running Claude session.
# Must be called AFTER claude_start has finished (splash gone).
# Uses tmux buffer paste for the text (avoids issues with tmux send-keys
# and long strings) then sends Enter as a separate key.
claude_prompt() {
    local prompt="$1"
    # Load text into tmux buffer and paste into pane — more reliable than
    # send-keys for long prompts.
    printf '%s' "$prompt" | tmux load-buffer -
    tmux paste-buffer -t "${TMUX_SESSION}"
    sleep 0.5
    tmux send-keys -t "${TMUX_SESSION}" Enter
    sleep 0.5  # brief pause to let input register
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
skip() { echo "    ⊘ SKIP: $1"; SKIP=$(( SKIP + 1 )); }

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
    local skip_suffix=""
    if [[ $SKIP -gt 0 ]]; then
        skip_suffix=", ${SKIP} skipped"
    fi
    echo " Results: ${PASS} passed, ${FAIL} failed${skip_suffix}"
    echo "══════════════════════════════════"
    [[ $FAIL -eq 0 ]]
}
