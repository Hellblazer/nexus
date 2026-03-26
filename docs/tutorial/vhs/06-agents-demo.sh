#!/usr/bin/env bash
# Section 6: Agents and Skills — Debug + Review Demos
#
# Drives Claude Code via tmux to demonstrate:
#   - /nx:debug with a failing test scenario
#   - /nx:review-code on a file with a bare except/pass
#
# Prerequisites:
#   - container-setup.sh has been run
#   - tmux session "tutorial" with Claude Code logged in
#   - error_handler.py exists in demo-repo (has the bare except/pass)
#
# Usage:
#   ./06-agents-demo.sh

set -euo pipefail
source "$(dirname "$0")/tmux-helpers.sh"

section_header "Section 6: Agents and Skills"

# --- 6.1: Debug demo ---
echo "→ Debug demo: intermittent test failure"
wait_for_prompt
pause 2

send "/nx:debug The test test_retry_on_timeout is failing intermittently. Sometimes it passes, sometimes it times out after 30 seconds."
# Debug agent runs on Opus — can take 60-120s
echo "  (Opus agent — expect 60-120s)"
wait_for_prompt
pause 3

# --- 6.2: Code review demo ---
# The error_handler.py file already has the bare except/pass from container-setup.sh
# Make a small uncommitted change to trigger the review
echo "→ Making uncommitted change for review demo..."
tmux send-keys -t "${TMUX_SESSION}:${TMUX_PANE}" "" # ensure we're at prompt

# Stage the bad code change (error_handler.py already has it, but let's
# make sure there's something uncommitted for the reviewer to find)
send "!echo '# Added for demo' >> error_handler.py && git diff"
wait_for_prompt
pause 2

echo "→ Code review demo"
send "/nx:review-code"
# Sonnet agent — typically 30-60s
echo "  (Sonnet agent — expect 30-60s)"
wait_for_prompt
pause 3

section_header "Section 6 Complete"
echo "Next: ./07-rdr-demo.sh"
