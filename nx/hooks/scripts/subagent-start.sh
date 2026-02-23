#!/bin/bash

# SubagentStart Hook
# Injects context when agents spawn

# Inject PM context via nx pm resume + status if available
if command -v nx &> /dev/null; then
  RESUME=$(nx pm resume 2>/dev/null)
  STATUS=$(nx pm status 2>/dev/null)
  if [[ -n "$RESUME" || -n "$STATUS" ]]; then
    echo "## PM Context"
    [[ -n "$RESUME" ]] && echo "$RESUME" && echo ""
    [[ -n "$STATUS" ]] && echo "$STATUS" && echo ""
  fi
else
  # nx not available — skip PM context injection gracefully
  true
fi

# Show available T2 memory docs for active project
if command -v nx &> /dev/null && command -v git &> /dev/null; then
  PROJECT=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
  if [[ -n "$PROJECT" ]]; then
    T2_LIST=$(nx memory list --project "${PROJECT}_active" 2>/dev/null | head -8)
    if [[ -n "$T2_LIST" ]]; then
      echo "## T2 Memory (${PROJECT}_active)"
      echo "$T2_LIST"
      echo ""
    fi
  fi
fi

# Show active beads
if command -v bd &> /dev/null; then
  ACTIVE=$(bd list --status=in_progress 2>/dev/null | head -1)
  if [[ -n "$ACTIVE" ]]; then
    echo "Active Bead: $ACTIVE"
  fi
fi

# T1 scratch: session-scoped, each subagent has its own T1 scope
# Use nx memory for cross-agent relay within the same project session
