---
description: Archive active project (synthesize to T3, start T2 90-day decay) (project)
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

  # Verify there is an active project to archive
  if ! nx pm status &> /dev/null 2>&1; then
    echo "Error: No active project to archive"
    echo ""
    echo "Use /pm-list to see available projects."
    exit 1
  fi

  echo "# Archiving Project"
  echo ""

  # Show current state before archiving
  nx pm status 2>&1
  echo ""

  # Run nx pm archive - synthesizes to T3, starts 90-day T2 decay
  nx pm archive 2>&1
  ARCHIVE_EXIT=$?

  if [ $ARCHIVE_EXIT -eq 0 ]; then
    echo ""
    echo "---"
    echo ""
    echo "**Project archived.** PM documents synthesized to T3 knowledge store."
    echo "T2 entries will expire after 90 days (restorable within that window)."
    echo ""
    echo "**Next steps**:"
    echo "- \`/pm-new <name>\` - Start a new project"
    echo "- \`/pm-restore <name>\` - Restore this project within 90 days"
    echo "- \`/pm-list\` - See all projects"
    echo ""
    echo "**Note**: Beads are not affected by archiving."
  else
    echo ""
    echo "Error: nx pm archive failed."
    exit 1
  fi
}
