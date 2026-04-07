#!/bin/bash
# Auto-approve sn plugin MCP tools (Serena + Context7) — explicit full tool names.
set -euo pipefail

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('tool_name',''))" 2>/dev/null || echo "")

case "$TOOL_NAME" in
  mcp__plugin_sn_serena__check_onboarding_performed|\
  mcp__plugin_sn_serena__delete_memory|\
  mcp__plugin_sn_serena__edit_memory|\
  mcp__plugin_sn_serena__find_file|\
  mcp__plugin_sn_serena__initial_instructions|\
  mcp__plugin_sn_serena__insert_after_symbol|\
  mcp__plugin_sn_serena__insert_before_symbol|\
  mcp__plugin_sn_serena__jet_brains_find_declaration|\
  mcp__plugin_sn_serena__jet_brains_find_implementations|\
  mcp__plugin_sn_serena__jet_brains_find_referencing_symbols|\
  mcp__plugin_sn_serena__jet_brains_find_symbol|\
  mcp__plugin_sn_serena__jet_brains_get_symbols_overview|\
  mcp__plugin_sn_serena__jet_brains_inline_symbol|\
  mcp__plugin_sn_serena__jet_brains_move|\
  mcp__plugin_sn_serena__jet_brains_rename|\
  mcp__plugin_sn_serena__jet_brains_safe_delete|\
  mcp__plugin_sn_serena__jet_brains_type_hierarchy|\
  mcp__plugin_sn_serena__list_dir|\
  mcp__plugin_sn_serena__list_memories|\
  mcp__plugin_sn_serena__onboarding|\
  mcp__plugin_sn_serena__read_memory|\
  mcp__plugin_sn_serena__rename_memory|\
  mcp__plugin_sn_serena__replace_symbol_body|\
  mcp__plugin_sn_serena__search_for_pattern|\
  mcp__plugin_sn_serena__write_memory|\
  mcp__plugin_sn_context7__resolve-library-id|\
  mcp__plugin_sn_context7__query-docs)
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
