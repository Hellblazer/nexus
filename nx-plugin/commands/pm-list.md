---
description: List current and archived PM projects (project)
---

!{
  # Check if nx is available
  if ! command -v nx &> /dev/null; then
    echo "Error: nx is not installed or not in PATH"
    echo ""
    echo "Install Nexus to use project management commands."
    exit 1
  fi

  echo "# Project Management"
  echo ""

  # Active project
  echo "## Active Project"
  echo ""
  if nx pm status &> /dev/null 2>&1; then
    nx pm status 2>&1
  else
    echo "(none)"
    echo ""
    echo "Use /pm-new <name> to start a new project."
  fi

  echo ""
  echo "## Archived Projects (restorable within 90 days of archiving)"
  echo ""

  # nx pm reference lists T3 archived PM syntheses; also covers 90-day T2 window
  ARCHIVED=$(nx pm reference 2>/dev/null)
  if [ -n "$ARCHIVED" ]; then
    echo "$ARCHIVED"
  else
    echo "(no archived PM projects found)"
    echo ""
    echo "Use \`/pm-new <name>\` to start a new project, or"
    echo "  \`nx pm reference \"<topic>\"\` to search past work by keyword."
  fi

  echo ""
  echo "---"
  echo ""
  echo "**Commands**:"
  echo "- \`/pm-status\` - Detailed status of active project"
  echo "- \`/pm-archive\` - Archive active project to T3"
  echo "- \`/pm-restore <name>\` - Restore a project within its 90-day window"
  echo "- \`/pm-new <name>\` - Initialize a new project"
}
