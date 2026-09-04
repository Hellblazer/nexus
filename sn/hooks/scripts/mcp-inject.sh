#!/bin/bash

# sn SubagentStart hook — inject Serena + Context7 MCP tool guidance
# into every subagent. Timeout: 5s (hooks.json); two cats plus one python3.
#
# DELIVERY CONTRACT (Claude Code SubagentStart): emit content via the JSON
# envelope of the form
#   {"hookSpecificOutput": {"hookEventName": "SubagentStart",
#                            "additionalContext": "<text>"}}
# Plain stdout was the prior shape and once worked, but the JSON envelope is
# the documented schema — it makes the emit intent unambiguous, so a Claude
# Code change that tightens parsing won't silently drop the content. This
# mirrors conexus/hooks/scripts/subagent-start.sh, which migrated 2026-05-05
# (commit 68854ca). The sn plugin missed that migration; restoring it here
# is what gets Serena + Context7 setup back into spawned subagents.
#
# Implementation: capture all body stdout into a tempfile via FD redirection
# at the top of the script, then emit the JSON envelope at the end via an
# EXIT trap. Body code below stays unchanged and continues to use cat for
# content generation.

_SN_HOOK_OUTBUF=$(mktemp -t sn-subagent-start.XXXXXX) || _SN_HOOK_OUTBUF=""
if [[ -n "$_SN_HOOK_OUTBUF" ]]; then
    exec 3>&1 1>"$_SN_HOOK_OUTBUF"
fi
_sn_emit_json_envelope() {
    local rc=$?
    if [[ -n "$_SN_HOOK_OUTBUF" ]]; then
        exec 1>&3 3>&-
        python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SubagentStart",
        "additionalContext": sys.stdin.read(),
    },
}))
' < "$_SN_HOOK_OUTBUF"
        rm -f "$_SN_HOOK_OUTBUF"
    fi
    return $rc
}
trap _sn_emit_json_envelope EXIT

# Both sections are injected for every subagent (nexus-jbt5x). The former
# task-text heuristic skipped Serena for any prompt containing "investigate",
# "audit", "package", "dependency" or "migrate", which is most debugger and
# developer briefs; the two sections together are about 3 KB, cheaper than
# one subagent re-deriving a tool name. stdin is drained so the harness
# never sees a broken pipe.
cat >/dev/null

# Section bodies live in sibling .md files rather than heredocs.
# Bash here-docs hang in some non-interactive shell contexts (Claude Code
# harness, test subprocess fixtures) where the parent's stdin is wired to
# a pipe the here-doc machinery never closes — symptom is rc=124 timeout
# with empty output. Reading from a real file via cat has no such
# dependency, and the markdown stays editable as markdown.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cat "$SCRIPT_DIR/serena-section.md"
cat "$SCRIPT_DIR/context7-section.md"
