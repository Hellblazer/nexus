---
description: List current PM project and status (project)
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
  echo "---"
  echo ""
  echo "**Commands**:"
  echo "- \`/pm-status\` - Detailed status of active project"
  echo "- \`/pm-new <name>\` - Initialize a new project"
}
