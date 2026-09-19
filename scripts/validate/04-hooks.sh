#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Exercise each hook script directly with fake CC event JSON.
# Claude Code hook scripts read JSON from stdin; env is set via CLAUDE_*.
#
# Requires: lib.sh sourced, SANDBOX exported.

source "$(dirname "$0")/lib.sh"

REPO=$(git rev-parse --show-toplevel)
HOOKS="$REPO/conexus/hooks/scripts"

# Fake Claude Code hook env
export CLAUDE_PLUGIN_ROOT="$REPO/conexus"
export CLAUDE_PROJECT_DIR="$SANDBOX"
# The ported hooks below reach the RDR-184 ledger through
# nexus.hooks.expectations, which resolves its state dir off XDG_STATE_HOME
# and falls back to $HOME/.local/state. Pin it into the sandbox: a
# validation run must not append rows to the operator's live ledger, and
# the stop/verification hooks read that same ledger to decide.
export XDG_STATE_HOME="$SANDBOX/.local/state"
mkdir -p "$XDG_STATE_HOME"

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

# RDR-215 bead nexus-q02nx.21 deleted the bash hooks this step used to run;
# each is now a module under nexus.hooks, dispatched in production as a
# `type: mcp_tool` entry in conexus/hooks/hooks.json. tests/e2e/lib/
# drive_hook.sh runs one of those modules from a shell, which is what lets
# this step keep smoke-testing them.
#
# THE `|| true` IS GONE, AND THAT IS THE POINT. With it, this step was
# vacuous: `run` treats a zero exit as a pass, and `|| true` made every
# line exit zero whatever happened. It had been passing `stop_failure_
# hook.sh` for some time against a file that does not exist under that
# name (the real one is stop_failure_hook.py) — 127, swallowed, reported
# green. Every module below returns exit 0 by contract (HookResult.
# exit_code is 0 for every hook verb; only the ledger verbs, which are not
# here, propagate their own), so a non-zero now means an import error or a
# crash in run(), which is exactly what a smoke step should catch.
DRIVE="$REPO/tests/e2e/lib/drive_hook.sh"

step "Hook modules (ported from bash at RDR-215)"
run "auto_approve"                  bash -c "echo '$(_pre_tool_event)' | '$DRIVE' auto_approve"
run "divergence_language_guard"     bash -c "echo '$(_prompt_event)' | '$DRIVE' divergence_language_guard"
run "post_compact"                  bash -c "echo '$(_session_event)' | '$DRIVE' post_compact"
run "pre_close_verification"        bash -c "echo '$(_stop_event)' | '$DRIVE' pre_close_verification"
run "stop_failure"                  bash -c "echo '$(_stop_event)' | '$DRIVE' stop_failure"
run "stop_verification"             bash -c "echo '$(_stop_event)' | '$DRIVE' stop_verification"
run "subagent_start"                bash -c "echo '{\"subagent_type\":\"developer\"}' | '$DRIVE' subagent_start"

step "hooks.json manifest"
run "hooks.json parses as JSON"     python3 -c "import json; json.load(open('$REPO/conexus/hooks/hooks.json'))"

summary "hooks"
[[ $FAIL -eq 0 ]] || exit 1
