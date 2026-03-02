#!/bin/bash
# MCP Health Check - Silent on success, warns on failures only
# Checks for essential tools/processes, not HTTP endpoints

failures=()

# Check bd (beads) CLI is available
if ! command -v bd &>/dev/null; then
    failures+=("bd CLI not found — install: https://github.com/BeadsProject/beads")
fi

# Check nx CLI is available
if ! command -v nx &>/dev/null; then
    failures+=("nx CLI not found — run 'uv tool install conexus' or add nx to PATH")
else
    # Run nx doctor to verify nx subsystems are healthy; suppress output on success
    if ! nx doctor &>/dev/null 2>&1; then
        failures+=("nx doctor reported issues — run 'nx doctor' for details")
    fi
fi


# Only output if there are failures
if [ ${#failures[@]} -gt 0 ]; then
    echo ""
    echo "**Tools Check**: ${#failures[@]} issue(s)"
    for f in "${failures[@]}"; do
        echo "- $f"
    done
fi

exit 0
