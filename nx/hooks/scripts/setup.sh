#!/bin/bash

# Repository Setup Hook
# Runs with: claude --init or claude --maintenance

echo '=== Repository Setup ==='

# 1. Check nx and indexing server
if command -v nx &> /dev/null; then
  echo 'Checking nx...'
  if nx doctor 2>/dev/null; then
    echo '✓ nx healthy'
  else
    echo '⚠ nx doctor reported issues — run "nx doctor" for details'
  fi
else
  echo '⚠ nx not found — install with: uv tool install conexus'
fi

echo ''

# 2. Check bead health
if command -v bd &> /dev/null; then
  echo ''
  echo 'Bead Status:'
  READY=$(bd list --status=ready 2>/dev/null | wc -l | xargs)
  BLOCKED=$(bd list --status=blocked 2>/dev/null | wc -l | xargs)
  IN_PROGRESS=$(bd list --status=in_progress 2>/dev/null | wc -l | xargs)
  echo "  Ready: $READY"
  echo "  In Progress: $IN_PROGRESS"
  echo "  Blocked: $BLOCKED"
else
  echo '⚠ bd not found — install from: https://github.com/BeadsProject/beads'
fi


echo '=== Setup Complete ==='
