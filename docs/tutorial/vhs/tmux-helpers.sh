#!/usr/bin/env bash
# Shared helper functions for tutorial recording scripts.
# Source this file: source ./tmux-helpers.sh
#
# Uses hash-based idle detection from nx cli-controller skill.
# Claude Code's persistent footer line confuses prompt-char detection,
# so we poll until the pane output stops changing instead.

TMUX_TARGET="tutorial:0.0"

# How long output must be stable before we consider Claude "done" (seconds)
IDLE_SECONDS=4
# Max wait before giving up (seconds)
MAX_WAIT=300

wait_idle() {
    # Wait until tmux pane output stops changing for IDLE_SECONDS.
    # This is the correct way to detect Claude Code idle state —
    # prompt-char detection fails because the footer always has ❯.
    local idle_secs="${1:-$IDLE_SECONDS}"
    local timeout="${2:-$MAX_WAIT}"
    local last_hash="" hash=""
    local start=$(date +%s) last_change=$(date +%s)

    echo "  ⏳ Waiting for idle (${idle_secs}s stability)..."
    while true; do
        local now=$(date +%s)
        if (( now - start > timeout )); then
            echo "  ⚠ Timeout after ${timeout}s — continuing"
            return 1
        fi
        hash=$(tmux capture-pane -t "$TMUX_TARGET" -p | md5sum | cut -d' ' -f1)
        if [[ "$hash" != "$last_hash" ]]; then
            last_hash="$hash"
            last_change=$now
        elif (( now - last_change >= idle_secs )); then
            local elapsed=$((now - start))
            echo "  ✓ Idle detected (${elapsed}s)"
            return 0
        fi
        sleep 0.5
    done
}

send() {
    # Send text to Claude Code pane and press Enter.
    tmux send-keys -t "$TMUX_TARGET" "$1" Enter
}

send_first() {
    # Send the FIRST command to Claude Code after splash screen.
    # Claude Code's splash screen swallows the first Enter.
    # Pattern: type text (no Enter), pause, then send bare Enter.
    tmux send-keys -t "$TMUX_TARGET" "$1"
    sleep 1
    tmux send-keys -t "$TMUX_TARGET" Enter
}

pause() {
    # Visual pause between commands (for pacing in the recording)
    local seconds="${1:-2}"
    echo "  ⏸ Pause ${seconds}s"
    sleep "$seconds"
}

section_header() {
    echo ""
    echo "══════════════════════════════════════"
    echo "  $1"
    echo "══════════════════════════════════════"
    echo ""
}
