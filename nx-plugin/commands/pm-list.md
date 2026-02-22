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
  echo "## Archived Projects (T2 — restorable within 90 days)"
  echo ""

  # List T2 memory entries tagged as PM projects
  nx memory list 2>&1 | grep -i "^pm::\|__pm__\|project:" | head -20 || true

  # If no filtered results, show broader memory list with note
  if ! nx memory list 2>&1 | grep -qi "pm::\|__pm__\|project:"; then
    echo "(no archived PM projects in T2)"
  fi

  echo ""
  echo "## T3 Archives (permanent — use reference search)"
  echo ""
  echo "Search permanent archives with:"
  echo "  nx pm reference \"<project-name or topic>\""
  echo ""
  echo "---"
  echo ""
  echo "**Commands**:"
  echo "- \`/pm-status\` - Detailed status of active project"
  echo "- \`/pm-archive\` - Archive active project to T3"
  echo "- \`/pm-restore <name>\` - Restore a project within its 90-day window"
  echo "- \`/pm-new <name>\` - Initialize a new project"
}
