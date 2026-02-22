---
description: Save intent and clear any previous context (user)
---

!{
  # Parse session name and task from arguments
  if [ -z "$ARGUMENTS" ]; then
    echo "Error: Session name and task description required"
    echo "Usage: /check <session-name> <task description>"
    echo "Example: /check chatsome-vision Fix TemporalSpatialSynchronizer tests"
    exit 1
  fi

  # Extract session name (first word) and task (rest)
  SESSION_NAME=$(echo "$ARGUMENTS" | awk '{print $1}')
  TASK_DESC=$(echo "$ARGUMENTS" | cut -d' ' -f2-)

  if [ -z "$TASK_DESC" ]; then
    echo "Error: Task description required after session name"
    echo "Usage: /check <session-name> <task description>"
    exit 1
  fi

  # Sanitize session name (convert / to -, remove special chars)
  SESSION_NAME=$(echo "$SESSION_NAME" | tr '/' '-' | tr -cd '[:alnum:]-_')

  # Ensure session directory exists
  SESSION_DIR=~/.claude/sessions/$SESSION_NAME
  mkdir -p "$SESSION_DIR"

  CONTEXT_FILE="$SESSION_DIR/context.txt"
  INTENT_FILE="$SESSION_DIR/intent.txt"

  # Get current directory with symlinks resolved
  CURRENT_DIR="$(pwd -P)"

  # Build comprehensive continuation prompt
  {
    echo "# Session Continuation: $SESSION_NAME"
    echo ""
    echo "## Task"
    echo "$TASK_DESC"
    echo ""
    echo "## Context"
    echo ""
    echo "**Working Directory:** \`$CURRENT_DIR\`"
    echo ""

    # Git status with error handling
    if git rev-parse --git-dir > /dev/null 2>&1; then
      echo "**Git Status:**"
      echo "\`\`\`"
      git status -sb 2>/dev/null || echo "Git status unavailable"
      echo ""
      git diff --stat 2>/dev/null | head -20 || true
      echo "\`\`\`"
      echo ""
    else
      echo "**Git Status:** Not a git repository"
      echo ""
    fi

    # Beads status with error handling
    echo "**Beads Status:**"
    echo "\`\`\`"
    if command -v bd &> /dev/null; then
      if bd ready --limit 5 2>/dev/null; then
        true
      else
        echo "No beads tasks available or beads not initialized"
      fi
    else
      echo "Beads not available (bd command not found)"
    fi
    echo "\`\`\`"
    echo ""

    # Recent files modified
    if git rev-parse --git-dir > /dev/null 2>&1; then
      echo "**Recent Files Modified:**"
      echo "\`\`\`"
      git status --short 2>/dev/null | head -10 || echo "N/A"
      echo "\`\`\`"
      echo ""
    fi

    echo "---"
    echo "*Saved at: $(date '+%Y-%m-%d %H:%M:%S')*"
  } > "$CONTEXT_FILE"

  # Check if write succeeded
  if [ ! -f "$CONTEXT_FILE" ]; then
    echo "Error: Failed to save context to $CONTEXT_FILE"
    exit 1
  fi

  # Save simple intent too
  echo "$TASK_DESC" > "$INTENT_FILE"

  echo "Session '$SESSION_NAME' saved"
}

Continuation prompt saved for session **$SESSION_NAME**:

```markdown
!cat "$SESSION_DIR/context.txt"
```

Next steps:
1. Type /clear to clear this session's context
2. Type **/load $SESSION_NAME** in a fresh session to continue

Or copy the prompt above manually if preferred.
