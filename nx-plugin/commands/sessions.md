---
description: List all saved sessions with metadata (user)
---

!{
  SESSIONS_DIR=~/.claude/sessions

  if [ ! -d "$SESSIONS_DIR" ]; then
    echo "No sessions found."
    echo ""
    echo "Use /check <session-name> <task> to create your first session."
    exit 0
  fi

  # Count sessions
  SESSION_COUNT=$(ls -1 "$SESSIONS_DIR" 2>/dev/null | wc -l | tr -d ' ')

  if [ "$SESSION_COUNT" -eq 0 ]; then
    echo "No sessions found."
    echo ""
    echo "Use /check <session-name> <task> to create your first session."
    exit 0
  fi

  echo "Found $SESSION_COUNT session(s):"
  echo ""

  # List each session with details
  for SESSION_DIR in "$SESSIONS_DIR"/*; do
    if [ -d "$SESSION_DIR" ]; then
      SESSION_NAME=$(basename "$SESSION_DIR")
      CONTEXT_FILE="$SESSION_DIR/context.txt"
      INTENT_FILE="$SESSION_DIR/intent.txt"

      echo "**$SESSION_NAME**"

      # Show task from intent file
      if [ -f "$INTENT_FILE" ]; then
        TASK=$(head -1 "$INTENT_FILE")
        echo "  Task: $TASK"
      fi

      # Show age and timestamp
      if [ -f "$CONTEXT_FILE" ]; then
        if [ "$(uname)" = "Darwin" ]; then
          # macOS
          TIMESTAMP=$(stat -f %Sm -t '%Y-%m-%d %H:%M:%S' "$CONTEXT_FILE" 2>/dev/null)
          FILE_AGE=$(( ($(date +%s) - $(stat -f %m "$CONTEXT_FILE")) / 86400 ))
        else
          # Linux
          TIMESTAMP=$(stat -c %y "$CONTEXT_FILE" 2>/dev/null | cut -d' ' -f1-2)
          FILE_AGE=$(( ($(date +%s) - $(stat -c %Y "$CONTEXT_FILE")) / 86400 ))
        fi

        if [ "$FILE_AGE" -eq 0 ]; then
          echo "  Saved: $TIMESTAMP (today)"
        elif [ "$FILE_AGE" -eq 1 ]; then
          echo "  Saved: $TIMESTAMP (1 day ago)"
        else
          echo "  Saved: $TIMESTAMP ($FILE_AGE days ago)"
        fi
      fi

      # Extract working directory from context
      if [ -f "$CONTEXT_FILE" ]; then
        WD=$(grep "^**Working Directory:**" "$CONTEXT_FILE" | sed -n 's/.*`\([^`]*\)`.*/\1/p')
        if [ -n "$WD" ]; then
          echo "  Directory: $WD"
        fi
      fi

      echo ""
    fi
  done

  echo "---"
  echo "Use /load <session-name> to continue a session"
  echo "Use /session-delete <session-name> to delete a session"
}
