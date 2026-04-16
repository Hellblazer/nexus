#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Exercise each hook script directly with fake CC event JSON.
# Claude Code hook scripts read JSON from stdin; env is set via CLAUDE_*.
#
# Requires: lib.sh sourced, SANDBOX exported.

source "$(dirname "$0")/lib.sh"

REPO=$(git rev-parse --show-toplevel)
HOOKS="$REPO/nx/hooks/scripts"

# Fake Claude Code hook env
export CLAUDE_PLUGIN_ROOT="$REPO/nx"
export CLAUDE_PROJECT_DIR="$SANDBOX"

# Fake SessionStart event payload
_session_event() {
    cat <<'JSON'
{"session_id": "validate-session-001", "transcript_path": "/tmp/nope", "cwd": "/tmp"}
JSON
}

# Fake UserPromptSubmit event
_prompt_event() {
    cat <<'JSON'
{"session_id": "validate-session-001", "prompt": "hello world"}
JSON
}

# Fake PreToolUse event (for verification hooks)
_pre_tool_event() {
    cat <<'JSON'
{"session_id":"s","tool_name":"Write","tool_input":{"file_path":"/tmp/x","content":"y"}}
JSON
}

# Fake SessionEnd / Stop event
_stop_event() {
    cat <<'JSON'
{"session_id":"s","reason":"normal"}
JSON
}

step "Python hooks"
run "session_start_hook.py"        bash -c "echo '$(_session_event)' | python3 '$HOOKS/session_start_hook.py'"
run "rdr_hook.py"                  bash -c "echo '$(_session_event)' | python3 '$HOOKS/rdr_hook.py'"
run "t2_prefix_scan.py"            bash -c "echo '$(_pre_tool_event)' | python3 '$HOOKS/t2_prefix_scan.py' || true"

step "Bash hooks"
run "auto-approve-nx-mcp.sh"        bash -c "echo '$(_pre_tool_event)' | bash '$HOOKS/auto-approve-nx-mcp.sh' || true"
run "divergence-language-guard.sh"  bash -c "echo '$(_prompt_event)' | bash '$HOOKS/divergence-language-guard.sh' || true"
run "post_compact_hook.sh"          bash -c "echo '$(_session_event)' | bash '$HOOKS/post_compact_hook.sh' || true"
run "pre_close_verification_hook.sh" bash -c "echo '$(_stop_event)' | bash '$HOOKS/pre_close_verification_hook.sh' || true"
run "stop_failure_hook.sh"          bash -c "echo '$(_stop_event)' | bash '$HOOKS/stop_failure_hook.sh' || true"
run "stop_verification_hook.sh"     bash -c "echo '$(_stop_event)' | bash '$HOOKS/stop_verification_hook.sh' || true"
run "subagent-start.sh"             bash -c "echo '{\"subagent_type\":\"developer\"}' | bash '$HOOKS/subagent-start.sh' || true"

step "hooks.json manifest"
run "hooks.json parses as JSON"     python3 -c "import json; json.load(open('$REPO/nx/hooks/hooks.json'))"

summary "hooks"
[[ $FAIL -eq 0 ]] || exit 1
