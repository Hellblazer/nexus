#!/bin/bash

# SubagentStart Hook
# Injects context when agents spawn

# Inject PM context via nx pm resume if available
if command -v nx &> /dev/null; then
  RESUME=$(nx pm resume 2>/dev/null)
  if [[ -n "$RESUME" ]]; then
    echo "## PM Context"
    echo "$RESUME"
    echo ""
  fi
else
  # nx not available — skip PM context injection gracefully
  true
fi

# Show active beads
if command -v bd &> /dev/null; then
  ACTIVE=$(bd list --status=in_progress 2>/dev/null | head -1)
  if [[ -n "$ACTIVE" ]]; then
    echo "Active Bead: $ACTIVE"
  fi
fi
