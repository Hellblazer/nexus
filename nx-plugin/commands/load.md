---
description: Load saved continuation (context or intent) (user)
---

!{
  # Validate session name argument
  if [ -z "$ARGUMENTS" ]; then
    echo "Error: Session name required"
    echo "Usage: /load <session-name>"
    echo "Example: /load chatsome-vision"
    echo ""
    echo "Available sessions:"
    if [ -d ~/.claude/sessions ]; then
      ls -1 ~/.claude/sessions | sed 's/^/  - /'
    else
      echo "  (none yet - use /check to create one)"
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
      echo "  (none yet)"
    fi
    exit 1
  fi

  CONTEXT_FILE="$SESSION_DIR/context.txt"
  INTENT_FILE="$SESSION_DIR/intent.txt"

  if [ -f "$CONTEXT_FILE" ]; then
    # Check if continuation is stale (older than 7 days)
    if [ "$(uname)" = "Darwin" ]; then
      # macOS
      FILE_AGE=$(( ($(date +%s) - $(stat -f %m "$CONTEXT_FILE")) / 86400 ))
    else
      # Linux
      FILE_AGE=$(( ($(date +%s) - $(stat -c %Y "$CONTEXT_FILE")) / 86400 ))
    fi

    if [ "$FILE_AGE" -gt 7 ]; then
      echo "Warning: Continuation is $FILE_AGE days old (saved $(stat -f %Sm -t '%Y-%m-%d %H:%M:%S' "$CONTEXT_FILE" 2>/dev/null || stat -c %y "$CONTEXT_FILE" 2>/dev/null | cut -d' ' -f1-2))"
      echo ""
    fi

    # Extract working directory safely
    WD=$(grep "^**Working Directory:**" "$CONTEXT_FILE" | sed -n 's/.*`\([^`]*\)`.*/\1/p')

    if [ -n "$WD" ] && [ -d "$WD" ]; then
      if cd "$WD" 2>/dev/null; then
        echo "Changed to: $WD"
        echo ""
      else
        echo "Warning: Could not change to directory: $WD"
        echo ""
      fi
    elif [ -n "$WD" ]; then
      echo "Warning: Saved directory no longer exists: $WD"
      echo ""
    fi

    # Display the full continuation prompt
    cat "$CONTEXT_FILE"

  elif [ -f "$INTENT_FILE" ]; then
    # Check if intent is stale
    if [ "$(uname)" = "Darwin" ]; then
      FILE_AGE=$(( ($(date +%s) - $(stat -f %m "$INTENT_FILE")) / 86400 ))
    else
      FILE_AGE=$(( ($(date +%s) - $(stat -c %Y "$INTENT_FILE")) / 86400 ))
    fi

    if [ "$FILE_AGE" -gt 7 ]; then
      echo "Warning: Intent is $FILE_AGE days old"
      echo ""
    fi

    echo "# Session Continuation"
    echo ""
    echo "## Task"
    cat "$INTENT_FILE"
    echo ""
    echo "**Working Directory:** \`$(pwd -P)\`"
    echo ""
    echo "(Simple intent only - use /check-full for more context)"

  else
    echo "Error: No continuation found for session '$SESSION_NAME'."
    echo ""
    echo "Use /check $SESSION_NAME <task> to save a continuation point first."
    exit 1
  fi
}

Ready to continue session **$SESSION_NAME**.
