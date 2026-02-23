---
description: Mark project complete, archive, and close (project)
---

!{
  # Check if nx is available
  if ! command -v nx &> /dev/null; then
    echo "Error: nx is not installed or not in PATH"
    echo ""
    echo "Install Nexus to use project management commands."
    exit 1
  fi

  # Verify there is an active project
  if ! nx pm status &> /dev/null 2>&1; then
    echo "Error: No active project to close"
    echo ""
    echo "Use /pm-list to see available projects."
    exit 1
  fi

  echo "# Closing Project"
  echo ""

  # Show current state before closing
  nx pm status 2>&1
  echo ""

  # Run nx pm close - archives and marks completed
  nx pm close 2>&1
  CLOSE_EXIT=$?

  if [ $CLOSE_EXIT -eq 0 ]; then
    echo ""
    echo "---"
    echo ""
    echo "**Project closed successfully.**"
    echo "PM documents synthesized to T3 knowledge store."
    echo "T2 entries will expire after 90 days (restorable within that window)."
    echo ""
    echo "**Next steps**:"
    echo "- \`/pm-new <name>\` - Start a new project"
    echo "- \`/pm-list\` - View all projects"
    echo "- \`/pm-restore <name>\` - Restore if needed (within 90-day window)"
    echo ""
    echo "**Beads note**: Epic and related beads are not closed automatically."
    echo "Close them manually if desired: \`bd close <epic-id>\`"
  else
    echo ""
    echo "Error: nx pm close failed."
    exit 1
  fi
}
