---
description: Restore an archived project within its 90-day decay window (project)
disable-model-invocation: true
---

!{
  # Check if nx is available
  if ! command -v nx &> /dev/null; then
    echo "Error: nx is not installed or not in PATH"
    echo ""
    echo "Install Nexus to use project management commands."
    exit 1
  fi

  # Check for project name argument
  if [ -z "$ARGUMENTS" ]; then
    echo "Error: Project name required"
    echo ""
    echo "Usage: /pm-restore <project-name>"
    echo ""
    echo "Use /pm-list to see restorable projects."
    exit 1
  fi

  PROJECT_NAME="$ARGUMENTS"

  # Check if there's already an active project
  if nx pm status &> /dev/null 2>&1; then
    echo "Error: Active project already exists."
    echo ""
    nx pm status 2>&1
    echo ""
    echo "Archive or close the current project first:"
    echo "  /pm-archive"
    echo "  /pm-close"
    exit 1
  fi

  echo "# Restoring Project: $PROJECT_NAME"
  echo ""

  # Run nx pm restore
  nx pm restore "$PROJECT_NAME" 2>&1
  RESTORE_EXIT=$?

  if [ $RESTORE_EXIT -eq 0 ]; then
    echo ""
    echo "---"
    echo ""
    echo "**Project restored.** T2 documents are active again."
    echo ""
    echo "**Next steps**:"
    echo "- \`/pm-status\` - View detailed project status"
    echo "- \`nx pm status\` - View project status (PM context auto-injected by hooks)"
    echo "- \`bd ready\` - See available work"
  else
    echo ""
    echo "Error: nx pm restore failed."
    echo ""
    echo "The project may have already expired (beyond the 90-day window)."
    echo "Use \`nx pm reference \"$PROJECT_NAME\"\` to search T3 archives for past work."
    exit 1
  fi
}
