#!/bin/bash

# PermissionRequest Hook (stdin JSON version)
# Auto-approves safe commands to reduce prompts
#
# Input: JSON on stdin with tool request data
# Output: 'allow', 'deny', or nothing (ask user)

# Read JSON from stdin
INPUT=$(cat)

# Extract tool and command from JSON
# (Actual JSON structure TBD - test in Phase 0)
TOOL=$(echo "$INPUT" | jq -r '.tool // empty' 2>/dev/null)
COMMAND=$(echo "$INPUT" | jq -r '.command // empty' 2>/dev/null)

# Defense in depth: Deny dangerous commands even if wildcards match
if [[ "$TOOL" == "Bash" ]]; then

  # Deny destructive Maven Wrapper commands
  if [[ "$COMMAND" =~ ^\.\/mvnw\ (deploy|release:|site:deploy) ]]; then
    echo "deny"
    exit 0
  fi

  # Deny destructive bead commands
  if [[ "$COMMAND" =~ ^bd\ (delete|sync\ --force) ]]; then
    echo "deny"
    exit 0
  fi

  # Deny destructive git commands
  if [[ "$COMMAND" =~ ^git\ (push\ --force|reset\ --hard|clean\ -f) ]]; then
    echo "deny"
    exit 0
  fi

fi

# Auto-approve read-only bead commands
if [[ "$TOOL" == "Bash" ]]; then

  # Bead commands (safe - just task management)
  # Note: bd delete NOT included (destructive)
  if [[ "$COMMAND" =~ ^bd\ (list|show|search|prime|ready|status) ]]; then
    echo "allow"
    exit 0
  fi

  # Read-only git commands
  if [[ "$COMMAND" =~ ^git\ (log|diff|status|show|branch\ -a|remote\ -v) ]]; then
    echo "allow"
    exit 0
  fi

  # Maven dry-run / info commands (no deploy/install)
  if [[ "$COMMAND" =~ ^mvn\ (help:|dependency:tree|dependency:analyze|versions:display) ]]; then
    echo "allow"
    exit 0
  fi

  # Nexus read-only commands
  if [[ "$COMMAND" =~ ^nx\ (search|store\ list|store\ get|memory\ list|memory\ get|memory\ search|scratch\ list|pm\ status|pm\ list|doctor|health|index) ]]; then
    echo "allow"
    exit 0
  fi

fi

# Default: ask user
# (no output = prompt user)
