#!/usr/bin/env bash
# Shared helper functions for tutorial recording scripts.
# Source this file: source ./tmux-helpers.sh

TMUX_SESSION="tutorial"
TMUX_PANE="0"
PROMPT_CHAR="❯"

# How often to poll for prompt (seconds)
POLL_INTERVAL=2
# Max wait before giving up (seconds)
MAX_WAIT=300

send() {
    # Send text to the Claude Code pane
    tmux send-keys -t "${TMUX_SESSION}:${TMUX_PANE}" "$1" Enter
}

send_raw() {
    # Send text without pressing Enter (for multi-line input)
    tmux send-keys -t "${TMUX_SESSION}:${TMUX_PANE}" "$1"
}

wait_for_prompt() {
    # Wait until the Claude Code prompt appears (❯ at end of last line)
    # This indicates Claude has finished responding.
    local elapsed=0
    echo "  ⏳ Waiting for prompt..."
    while true; do
        local last_line
        last_line=$(tmux capture-pane -t "${TMUX_SESSION}:${TMUX_PANE}" -p | \
                    grep -v '^$' | tail -1)
        if echo "$last_line" | grep -q "${PROMPT_CHAR}"; then
            echo "  ✓ Prompt detected (${elapsed}s)"
            return 0
        fi
        sleep "$POLL_INTERVAL"
        elapsed=$((elapsed + POLL_INTERVAL))
        if [ "$elapsed" -ge "$MAX_WAIT" ]; then
            echo "  ⚠ Timeout after ${MAX_WAIT}s — continuing anyway"
            return 1
        fi
    done
}

pause() {
    # Visual pause between commands (for pacing in the recording)
    local seconds="${1:-2}"
    echo "  ⏸ Pause ${seconds}s"
    sleep "$seconds"
}

section_header() {
    # Print a section header to the control terminal
    echo ""
    echo "══════════════════════════════════════"
    echo "  $1"
    echo "══════════════════════════════════════"
    echo ""
}

start_recording() {
    # Start asciinema recording of the Claude Code pane
    local output_file="${1:-recording.cast}"
    echo "🔴 Starting recording: ${output_file}"
    # Record the tmux pane by piping its output
    asciinema rec --command "tmux attach-session -t ${TMUX_SESSION}" \
        "$output_file" &
    ASCIINEMA_PID=$!
    sleep 2
}

stop_recording() {
    # Stop asciinema recording
    if [ -n "${ASCIINEMA_PID:-}" ]; then
        kill "$ASCIINEMA_PID" 2>/dev/null || true
        echo "⏹ Recording stopped"
    fi
}
