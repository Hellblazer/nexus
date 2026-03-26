#!/usr/bin/env bash
# Section 5: Nexus Inside Claude
#
# Drives Claude Code via tmux to demonstrate:
#   - Automatic search (Claude searches indexed code before answering)
#   - Storing decisions in memory
#   - Retrieving stored decisions
#   - Agent coordination via scratch
#
# Prerequisites:
#   - container-setup.sh has been run
#   - tmux session "tutorial" running with Claude Code logged in
#   - Demo repo indexed and memory populated
#
# Usage:
#   ./05-nexus-in-claude.sh

set -euo pipefail
source "$(dirname "$0")/tmux-helpers.sh"

section_header "Section 5: Nexus Inside Claude"

# --- 5.1: Automatic search ---
echo "→ Automatic search demo"
wait_idle
pause 2

# First command — use send_first to handle splash screen
send_first "How does the retry logic work in this project?"
wait_idle
pause 3

# --- 5.2: Store a decision ---
echo "→ Store a decision"
send "Remember that we decided to use connection pooling with a max of 10 connections for the database layer."
wait_idle
pause 2

# --- 5.3: Retrieve the decision ---
echo "→ Retrieve the decision"
send "What do we know about the database configuration?"
wait_idle
pause 3

# --- 5.4: Multi-agent coordination ---
echo "→ Multi-agent coordination"
send "Search the codebase for how errors are handled, and also check if there are any error-related tests."
wait_idle
pause 3

section_header "Section 5 Complete"
echo "Next: ./06-agents-demo.sh"
