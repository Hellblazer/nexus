#!/bin/bash
# PostToolUse divergence-language guard for RDR post-mortem files (RDR-065 Gap 2).
# Advisory only — never hard-blocks. Emits permissionDecision: allow with optional
# additionalContext describing pattern hits in the just-written file.
# SPDX-License-Identifier: AGPL-3.0-or-later

# No set -e/-u/-o pipefail — this hook must NEVER fail.

allow() {
    if [[ -n "${1:-}" ]]; then
        local escaped
        escaped=$(printf '%s' "$1" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$1")
        printf '{"hookSpecificOutput": {"hookEventName": "PostToolUse", "permissionDecision": "allow", "additionalContext": %s}}\n' "$escaped"
    else
        printf '{"hookSpecificOutput": {"hookEventName": "PostToolUse", "permissionDecision": "allow"}}\n'
    fi
    exit 0
}

STDIN=$(cat 2>/dev/null || true)
[[ -z "$STDIN" ]] && allow

# Fast no-op: tool_name must be Write or Edit.
TOOL_NAME=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try: print(json.load(sys.stdin).get('tool_name', ''))
except Exception: print('')
" 2>/dev/null || true)
[[ "$TOOL_NAME" != "Write" && "$TOOL_NAME" != "Edit" ]] && allow

# Fast no-op: file_path must be a post-mortem.
FILE_PATH=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('file_path', ''))
except Exception: print('')
" 2>/dev/null || true)
printf '%s' "$FILE_PATH" | grep -q 'docs/rdr/post-mortem/' || allow
[[ ! -f "$FILE_PATH" ]] && allow

# Apply pre-filter and run the locked Rev 4 8-pattern bank.
HITS=$(python3 - "$FILE_PATH" <<'PYEOF'
import sys, re
path = sys.argv[1]
bank = re.compile(
    r'divergence|workaround|limitation|deferred|follow-up\s+RDR|'
    r'Phase\s+\d+\s+(deferred|required)|out\s+of\s+scope|not\s+in\s+scope',
    re.IGNORECASE,
)
results = []
try:
    with open(path, encoding='utf-8', errors='replace') as f:
        for i, line in enumerate(f, 1):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if stripped.startswith('|') and stripped.endswith('|'):
                continue
            if bank.search(stripped):
                results.append(f"  line {i}: {stripped[:120]}")
except Exception:
    pass
for r in results:
    print(r)
PYEOF
)

[[ -z "$HITS" ]] && allow

# Log hit summary to T1 scratch for post-launch precision review.
if command -v nx &>/dev/null; then
    TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    HIT_COUNT=$(printf '%s\n' "$HITS" | grep -c '^' 2>/dev/null || printf '0')
    nx scratch put "divergence-hook hit: $TS file=$FILE_PATH hits=$HIT_COUNT" \
        --tags "divergence-hook-hit,precision-review" >/dev/null 2>&1 || true
fi

MSG="Divergence-language hits in $(basename "$FILE_PATH"):
$HITS

These may indicate acknowledged scope deferral (intended) or silent scope reduction (unintended). Review each hit and decide: is this a real divergence that should force close_reason=partial, or a legitimate acknowledged deferral?"

allow "$MSG"
