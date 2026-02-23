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
  echo "## Archived Projects (T3 permanent; restorable within 90-day T2 window)"
  echo ""

  # nx pm reference requires a query argument — no-arg mode prompts interactively
  # (not safe in a non-TTY slash command context). Display instructions instead.
  echo "Search archived projects with:"
  echo "  \`nx pm reference \"<project-name>\"\`       — retrieve by project name"
  echo "  \`nx pm reference \"<topic or question>\"\`  — semantic search across all archives"
  echo ""
  echo "Restore a project (within 90-day T2 decay window):"
  echo "  \`/pm-restore <project-name>\`"

  echo ""
  echo "---"
  echo ""
  echo "**Commands**:"
  echo "- \`/pm-status\` - Detailed status of active project"
  echo "- \`/pm-archive\` - Archive active project to T3"
  echo "- \`/pm-restore <name>\` - Restore a project within its 90-day window"
  echo "- \`/pm-new <name>\` - Initialize a new project"
}
