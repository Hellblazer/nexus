#!/bin/bash

# SubagentStart Hook
# Injects context when agents spawn

# Inject PM context via nx pm resume + status if available
if command -v nx &> /dev/null; then
  RESUME=$(nx pm resume 2>/dev/null)
  STATUS=$(nx pm status 2>/dev/null)
  if [[ -n "$RESUME" || -n "$STATUS" ]]; then
    echo "## PM Context"
    [[ -n "$RESUME" ]] && echo "$RESUME" && echo ""
    [[ -n "$STATUS" ]] && echo "$STATUS" && echo ""
  fi
else
  # nx not available — skip PM context injection gracefully
  true
fi

# Show available T2 memory docs for active project (all namespaces via prefix scan)
if command -v git &> /dev/null; then
  PROJECT=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
  if [[ -n "$PROJECT" ]]; then
    SCAN_SCRIPT="$CLAUDE_PLUGIN_ROOT/hooks/scripts/t2_prefix_scan.py"
    T2_OUT=$(uv run python "$SCAN_SCRIPT" "$PROJECT" 2>/dev/null)
    if [[ -n "$T2_OUT" ]]; then
      echo "## T2 Memory (Active Project)"
      echo "$T2_OUT"
      echo ""
    fi
  fi
fi

# Show active beads
if command -v bd &> /dev/null; then
  ACTIVE=$(bd list --status=in_progress 2>/dev/null | head -1)
  if [[ -n "$ACTIVE" ]]; then
    echo "Active Bead: $ACTIVE"
  fi
fi

# Inject relay template so skills don't need to duplicate it
RELAY_TEMPLATE="$CLAUDE_PLUGIN_ROOT/agents/_shared/RELAY_TEMPLATE.md"
if [[ -f "$RELAY_TEMPLATE" ]]; then
  echo ""
  echo "## Relay Format (injected by hook)"
  echo ""
  # Emit required-fields table and template (stop before Optional Fields)
  awk '/^## Optional Fields/{exit} {print}' "$RELAY_TEMPLATE"
fi

# T1 scratch: SHARED across all agents in this session via PPID chain (RDR-010).
# All agents spawned from the same root Claude Code process see the same entries.
# Inject current entries so this agent knows what siblings/parent already found.
if command -v nx &> /dev/null; then
  T1_ENTRIES=$(nx scratch list 2>/dev/null)
  if [[ -n "$T1_ENTRIES" && "$T1_ENTRIES" != "No scratch entries." ]]; then
    echo ""
    echo "## Session Scratch (T1 — shared across all agents this session)"
    echo "$T1_ENTRIES"
    echo ""
  fi
fi
