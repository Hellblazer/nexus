#!/bin/bash
# Auto-approve all nexus MCP tools — no permission prompts needed.
set -euo pipefail

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('tool_name',''))" 2>/dev/null || echo "")

if [[ "$TOOL_NAME" == mcp__plugin_nx_* ]]; then
  python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PermissionRequest',
        'decision': {'behavior': 'allow'}
    }
}))
"
fi
