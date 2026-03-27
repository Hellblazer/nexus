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
#   - error_handler.py has uncommitted bare except/pass (staged by container-setup)
#
# Usage:
#   ./06-agents-demo.sh

set -euo pipefail
source "$(dirname "$0")/tmux-helpers.sh"

section_header "Section 6: Agents and Skills"

# --- 6.1: Debug demo ---
echo "→ Debug demo: intermittent test failure"
wait_idle
pause 2

send "/nx:debug The test test_retry_on_timeout is failing intermittently. Sometimes it passes, sometimes it times out after 30 seconds."
# Debug agent runs on Opus — typically 60-120s
echo "  (Opus agent — expect 60-120s)"
wait_idle 6 300
pause 3

# --- 6.2: Code review demo ---
# error_handler.py already has the bare except/pass as an uncommitted change
# (staged by container-setup.sh). No shell escape needed.
echo "→ Code review demo"
wait_idle
pause 2

send "/nx:review-code"
# Sonnet agent — typically 30-60s
echo "  (Sonnet agent — expect 30-60s)"
wait_idle 6 180
pause 3

section_header "Section 6 Complete"
echo "Next: ./07-rdr-demo.sh"
