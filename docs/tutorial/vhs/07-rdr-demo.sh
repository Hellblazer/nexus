#!/usr/bin/env bash
# Section 7: The RDR Process — Create + Research Demo
#
# Drives Claude Code via tmux to demonstrate:
#   - /nx:rdr-create with a title
#   - /nx:rdr-research add with a finding
#   - /nx:rdr-list to show all RDRs
#   - Searching RDRs by meaning
#
# Prerequisites:
#   - container-setup.sh has been run
#   - tmux session "tutorial" with Claude Code logged in
#
# Usage:
#   ./07-rdr-demo.sh

set -euo pipefail
source "$(dirname "$0")/tmux-helpers.sh"

section_header "Section 7: The RDR Process"

# --- 7.1: Create an RDR ---
echo "→ Create RDR"
wait_for_prompt
pause 2

send "/nx:rdr-create API Rate Limiting Strategy"
wait_for_prompt
pause 3

# --- 7.2: Show the created file briefly ---
# The RDR was created in docs/rdr/ — open it
echo "→ Show created file"
send "!ls docs/rdr/rdr-*.md | tail -1 | xargs head -30"
wait_for_prompt
pause 3

# --- 7.3: Add a research finding ---
echo "→ Add research finding"
send "/nx:rdr-research add 001"
# Wait for Claude to process the command
sleep 3
# Now send the finding as a follow-up message
send "I checked the express-rate-limit package source code. It supports sliding window rate limiting with Redis backing, and has an in-memory fallback for single-process setups. Verified by reading the source."
wait_for_prompt
pause 3

# --- 7.4: List RDRs ---
echo "→ List RDRs"
send "/nx:rdr-list"
wait_for_prompt
pause 3

# --- 7.5: Search by meaning ---
echo "→ Search RDRs"
send "Search our previous decisions about rate limiting."
wait_for_prompt
pause 3

section_header "Section 7 Complete"
echo ""
echo "All demo sections recorded."
echo "Use asciinema or screen capture output for post-production."
