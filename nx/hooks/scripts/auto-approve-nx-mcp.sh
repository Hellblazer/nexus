#!/bin/bash
# Auto-approve nexus MCP tools — explicit full tool names, no wildcards.
# No set -e — a failed parse must not kill the hook (user just sees the prompt).

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('tool_name',''))" 2>/dev/null || echo "")

# Explicit allow list — every nx MCP tool by full name
case "$TOOL_NAME" in
  mcp__plugin_nx_nexus__search|\
  mcp__plugin_nx_nexus__query|\
  mcp__plugin_nx_nexus__store_put|\
  mcp__plugin_nx_nexus__store_get|\
  mcp__plugin_nx_nexus__store_list|\
  mcp__plugin_nx_nexus__memory_put|\
  mcp__plugin_nx_nexus__memory_get|\
  mcp__plugin_nx_nexus__memory_search|\
  mcp__plugin_nx_nexus__memory_delete|\
  mcp__plugin_nx_nexus__scratch|\
  mcp__plugin_nx_nexus__scratch_manage|\
  mcp__plugin_nx_nexus__collection_list|\
  mcp__plugin_nx_nexus__plan_save|\
  mcp__plugin_nx_nexus__plan_search|\
  mcp__plugin_nx_nexus-catalog__search|\
  mcp__plugin_nx_nexus-catalog__show|\
  mcp__plugin_nx_nexus-catalog__list|\
  mcp__plugin_nx_nexus-catalog__register|\
  mcp__plugin_nx_nexus-catalog__update|\
  mcp__plugin_nx_nexus-catalog__link|\
  mcp__plugin_nx_nexus-catalog__links|\
  mcp__plugin_nx_nexus-catalog__link_query|\
  mcp__plugin_nx_nexus-catalog__resolve|\
  mcp__plugin_nx_nexus-catalog__stats|\
  mcp__plugin_nx_sequential-thinking__sequentialthinking)
    python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PermissionRequest',
        'decision': {'behavior': 'allow'}
    }
}))
"
    ;;
esac
