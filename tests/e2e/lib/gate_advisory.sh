# SPDX-License-Identifier: AGPL-3.0-or-later
# The no-bare-green advisory for shell gates (nexus-1c7oq). Same line as
# src/nexus/gate_advisory.py's passed_by_default(); a summary counts them.
#
#   source tests/e2e/lib/gate_advisory.sh
#   passed_by_default <gate> <reason...>     # prints the line, exit 0 unchanged
#   $GATE_PASSED_BY_DEFAULT                  # how many times this shell said so
GATE_PASSED_BY_DEFAULT=${GATE_PASSED_BY_DEFAULT:-0}
passed_by_default() {
    local gate="$1"; shift
    echo "GATE PASSED-BY-DEFAULT: ${gate} $*"
    GATE_PASSED_BY_DEFAULT=$(( GATE_PASSED_BY_DEFAULT + 1 ))
}
