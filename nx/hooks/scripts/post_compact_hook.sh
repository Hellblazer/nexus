#!/bin/bash
# PostCompact Hook — re-inject in-progress bead state and scratch after compaction.
# SessionStart(compact) already re-injects skills, T2 memory, and bd ready.
# This hook adds what SessionStart doesn't cover: active work context.
# Output budget: ≤ 20 lines.

BODY=""

# In-progress beads
if command -v bd &> /dev/null; then
  ACTIVE=$(bd list --status=in_progress --limit=5 2>/dev/null)
  if [[ -n "$ACTIVE" ]]; then
    BODY+="### Active Work
\`\`\`
$(echo "$ACTIVE" | head -5)
\`\`\`
"
  fi
fi

# T1 scratch entries
if command -v nx &> /dev/null; then
  SCRATCH=$(nx scratch list 2>/dev/null)
  if [[ -n "$SCRATCH" && "$SCRATCH" != "No scratch entries." ]]; then
    BODY+="### Session Scratch (T1)
$(echo "$SCRATCH" | head -5)
"
  fi
fi

# Only emit header when there is content
if [[ -n "$BODY" ]]; then
  echo "## Post-Compaction Context"
  echo "$BODY"
fi

exit 0
