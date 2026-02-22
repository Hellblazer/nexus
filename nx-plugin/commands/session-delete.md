---
description: Delete a saved session (user)
---

!{
  if [ -z "$ARGUMENTS" ]; then
    echo "Error: Session name required"
    echo "Usage: /session-delete <session-name>"
    echo ""
    echo "Available sessions:"
    if [ -d ~/.claude/sessions ]; then
      ls -1 ~/.claude/sessions | sed 's/^/  - /'
    else
      echo "  (none)"
    fi
    exit 1
  fi

  # Sanitize session name
  SESSION_NAME=$(echo "$ARGUMENTS" | tr '/' '-' | tr -cd '[:alnum:]-_')
  SESSION_DIR=~/.claude/sessions/$SESSION_NAME

  if [ ! -d "$SESSION_DIR" ]; then
    echo "Error: Session '$SESSION_NAME' not found"
    echo ""
    echo "Available sessions:"
    if [ -d ~/.claude/sessions ]; then
      ls -1 ~/.claude/sessions | sed 's/^/  - /'
    else
      echo "  (none)"
    fi
    exit 1
  fi

  # Show what we're about to delete
  echo "Deleting session: $SESSION_NAME"
  if [ -f "$SESSION_DIR/intent.txt" ]; then
    TASK=$(head -1 "$SESSION_DIR/intent.txt")
    echo "Task: $TASK"
  fi
  echo ""

  # Delete the session directory
  rm -rf "$SESSION_DIR"

  if [ ! -d "$SESSION_DIR" ]; then
    echo "Session '$SESSION_NAME' deleted successfully"
  else
    echo "Error: Failed to delete session '$SESSION_NAME'"
    exit 1
  fi
}
