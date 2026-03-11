#!/bin/bash

# PermissionRequest Hook (stdin JSON version)
# Auto-approves safe commands to reduce prompts for subagents.
# Defense-in-depth: agent tools frontmatter defines what agents CAN use,
# this hook ensures they aren't silently denied by permission prompts.
#
# Input: JSON on stdin with tool request data (fields: .tool, .command)
# Output: 'allow', 'deny', or nothing (ask user)

# Read JSON from stdin
INPUT=$(cat)

# Extract tool and command from JSON
# Field names validated against production Claude Code PermissionRequest schema
TOOL=$(echo "$INPUT" | jq -r '.tool // empty' 2>/dev/null)
COMMAND=$(echo "$INPUT" | jq -r '.command // empty' 2>/dev/null)

# --- Auto-approve safe non-Bash tool types (RDR-023) ---

# Always safe: read-only local tools
if [[ "$TOOL" == "Read" || "$TOOL" == "Grep" || "$TOOL" == "Glob" ]]; then
  echo "allow"
  exit 0
fi

# Safe: local file write operations (controlled by agent tools list)
if [[ "$TOOL" == "Write" || "$TOOL" == "Edit" ]]; then
  echo "allow"
  exit 0
fi

# Safe: read-only external access (no mutations)
if [[ "$TOOL" == "WebSearch" || "$TOOL" == "WebFetch" ]]; then
  echo "allow"
  exit 0
fi

# Safe: orchestrator agent delegation
if [[ "$TOOL" == "Agent" ]]; then
  echo "allow"
  exit 0
fi

# Safe: sequential thinking MCP tool (reasoning primitive, no side effects)
if [[ "$TOOL" == "mcp__plugin_nx_sequential-thinking__sequentialthinking" ]]; then
  echo "allow"
  exit 0
fi

# Auto-approve all nexus MCP tools (storage tiers, search)
if [[ "$TOOL" =~ ^mcp__plugin_nx_nexus__ ]]; then
  echo "allow"
  exit 0
fi

# --- Bash tool: deny dangerous commands first, then allow safe ones ---

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

  # Deny destructive nx commands
  if [[ "$COMMAND" =~ ^nx\ collection\ delete ]]; then
    echo "deny"
    exit 0
  fi

fi

# Auto-approve safe Bash commands
if [[ "$TOOL" == "Bash" ]]; then

  # Bead commands (safe task management)
  # Note: bd delete NOT included (destructive, denied above)
  # Note: bd sync --force NOT included (denied above); plain sync IS safe
  if [[ "$COMMAND" =~ ^bd\ (list|show|search|prime|ready|status|create|update|close|dep|remember|memories|stats|doctor|sync) ]]; then
    echo "allow"
    exit 0
  fi

  # Read-only git commands
  # Note: branch and tag restricted to list/query forms only (no create/delete)
  if [[ "$COMMAND" =~ ^git\ (log|diff|status|show|rev-parse|describe|remote\ -v) ]]; then
    echo "allow"
    exit 0
  fi
  if [[ "$COMMAND" =~ ^git\ branch(\ (-a|-r|-v|-vv|--list))*$ ]]; then
    echo "allow"
    exit 0
  fi
  if [[ "$COMMAND" =~ ^git\ tag(\ (-l|--list))*$ ]]; then
    echo "allow"
    exit 0
  fi

  # Test runners
  if [[ "$COMMAND" =~ ^uv\ run\ pytest ]]; then
    echo "allow"
    exit 0
  fi

  # Maven dry-run / info commands (no deploy/install)
  if [[ "$COMMAND" =~ ^mvn\ (help:|dependency:tree|dependency:analyze|versions:display) ]]; then
    echo "allow"
    exit 0
  fi

  # Auto-approve all nx commands (nexus CLI)
  if [[ "$COMMAND" =~ ^nx($|[[:space:]]) ]]; then
    echo "allow"
    exit 0
  fi

fi

# Default: ask user
# (no output = prompt user)
