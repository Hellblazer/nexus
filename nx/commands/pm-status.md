---
description: Show current project status via nx pm (project)
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

  # Run nx pm status - this outputs phase, blockers, active agent context
  nx pm status 2>&1
  STATUS_EXIT=$?

  if [ $STATUS_EXIT -ne 0 ]; then
    echo ""
    echo "No active project found for this repository."
    echo ""
    echo "Use /pm-new to initialize a new project."
    echo "Use /pm-list to see available projects."
    exit 0
  fi

  echo ""
  echo "---"
  echo ""
  echo "**Commands**:"
  echo "- \`/pm-archive\` - Archive this project to T3 storage"
  echo "- \`/pm-close\` - Mark complete and archive"
  echo "- \`bd ready\` - See available work"
}
