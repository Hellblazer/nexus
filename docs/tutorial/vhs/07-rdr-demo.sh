#!/usr/bin/env bash
# Section 7: The RDR Process — Create + Research Demo
#
# Drives Claude Code via tmux to demonstrate:
#   - /nx:rdr-create with a title
#   - /nx:rdr-research add with a finding (inline, not interactive)
#   - /nx:rdr-list to show all RDRs
#   - Searching RDRs by meaning
#
# Prerequisites:
#   - container-setup.sh has been run (docs/rdr/ directory exists)
#   - tmux session "tutorial" with Claude Code logged in
#
# NOTE: bd (beads) is not installed. The rdr-create preamble will show
# "Beads not available" — this is expected and handled gracefully.
#
# NOTE: On re-runs, the RDR ID may not be 001. The container-setup.sh
# resets T2 memory but does not delete docs/rdr/ files from prior runs.
# For a clean recording, use a fresh container.
#
# Usage:
#   ./07-rdr-demo.sh

set -euo pipefail
source "$(dirname "$0")/tmux-helpers.sh"

section_header "Section 7: The RDR Process"

# --- 7.1: Create an RDR ---
echo "→ Create RDR"
wait_idle
pause 2

send "/nx:rdr-create API Rate Limiting Strategy"
wait_idle 6 120
pause 3

# --- 7.2: Show the created file briefly ---
echo "→ Show created file"
send "Show me the first 30 lines of the RDR file you just created."
wait_idle
pause 3

# --- 7.3: Add a research finding ---
# Use inline form — do NOT rely on interactive multi-field prompts.
echo "→ Add research finding"
send "/nx:rdr-research add 001 I checked the express-rate-limit package source code. It supports sliding window rate limiting with Redis backing, and has an in-memory fallback for single-process setups. Verified by reading the source."
wait_idle 6 120
pause 3

# --- 7.4: List RDRs ---
echo "→ List RDRs"
send "/nx:rdr-list"
wait_idle
pause 3

# --- 7.5: Search by meaning ---
echo "→ Search RDRs by meaning"
send "Search our previous decisions about rate limiting."
wait_idle
pause 3

section_header "Section 7 Complete"
echo ""
echo "All demo sections recorded."
echo "Stop your screen capture / asciinema now."
