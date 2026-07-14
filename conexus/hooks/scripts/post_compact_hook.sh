#!/bin/bash
# PostCompact Hook — re-inject in-progress bead state and scratch after compaction.
# SessionStart(compact) already re-injects skills, T2 memory, and bd ready.
# This hook adds what SessionStart doesn't cover: active work context.
# Output budget: ≤ 20 lines.

# nexus-7o1zh: export the harness-provided session_id for `nx scratch list`
# below. resolve_active_session_id()'s lowest-priority fallback is the
# machine-wide ~/.config/nexus/current_session flat file, clobbered
# unconditionally by ANY second top-level Claude Code session's SessionStart
# hook. This hook runs detached from any live nx-mcp process and cannot rely
# on env-var inheritance from a parent session, so it reads session_id
# directly out of its own stdin JSON payload (same pattern as
# pre_close_verification_hook.sh, nexus-36q84).
STDIN=$(cat 2>/dev/null || true)
HOOK_SESSION_ID=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || true)
if [[ -n "$HOOK_SESSION_ID" ]]; then
    export NX_SESSION_ID="$HOOK_SESSION_ID"
fi

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
