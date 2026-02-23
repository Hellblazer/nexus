---
description: Initialize project management for current repo via nx pm (project)
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
    echo "Usage: /pm-new <project-name>"
    echo ""
    echo "Example: /pm-new my-new-feature"
    exit 1
  fi

  PROJECT_NAME="$ARGUMENTS"

  # Check if a project is already active for this repo
  if nx pm status &> /dev/null 2>&1; then
    echo "# Active Project Exists"
    echo ""
    nx pm status 2>&1
    echo ""
    echo "You must archive or close the current project before starting a new one."
    echo ""
    echo "---"
    echo ""
    echo "**Options**:"
    echo ""
    echo "1. **Archive** (preserve current state):"
    echo "   \`\`\`"
    echo "   /pm-archive"
    echo "   /pm-new $PROJECT_NAME"
    echo "   \`\`\`"
    echo ""
    echo "2. **Close** (mark complete, synthesize to T3, then start fresh):"
    echo "   \`\`\`"
    echo "   /pm-close"
    echo "   /pm-new $PROJECT_NAME"
    echo "   \`\`\`"
    exit 0
  fi

  # Initialize PM for this repo
  echo "# Initializing Project: $PROJECT_NAME"
  echo ""

  nx pm init --project "$PROJECT_NAME" 2>&1
  INIT_EXIT=$?

  if [ $INIT_EXIT -ne 0 ]; then
    echo ""
    echo "Error: nx pm init failed. Check that nx is configured correctly."
    exit 1
  fi

  echo ""
  echo "**Project management initialized** for this repository."
  echo ""
  echo "---"
  echo ""
  echo "**To create project infrastructure**, invoke the project-management-setup agent:"
  echo ""
  echo "\`\`\`"
  echo "Please set up project management infrastructure for: $PROJECT_NAME"
  echo ""
  echo "Project type: [software/ml/infrastructure/research]"
  echo "Estimated duration: [weeks]"
  echo "Key phases: [list your phases]"
  echo "Success criteria: [what does done look like]"
  echo "\`\`\`"
  echo ""
  echo "The agent will:"
  echo "- Store PM phase documents in T2 (SQLite) via nx memory put"
  echo "- Create epic and phase beads in .beads/"
  echo "- Set up continuation context retrievable with nx pm resume"
  echo ""
  echo "---"
  echo ""
  echo "**Quick commands**:"
  echo "- \`nx pm status\` - Check project status"
  echo "- \`nx pm resume\` - Inject session context"
  echo "- \`bd ready\` - See available work"
}
