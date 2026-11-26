#!/usr/bin/env bash
# Scenario 35 — a PreToolUse hook that answers `permissionDecision: allow` and NOTHING else
# (no PermissionRequest hook at all), against a permissions.ask rule for the same MCP tool:
# does the rule still stop the call at a permission prompt, or does the hook's allow run it?
#
# RDR-259 (conexus project and user settings, nexus-sby49.15, step 4): Decision 9 moves the
# plugin's auto-approve off PermissionRequest, which overrides a user's `ask` rule silently
# (the defect: a user who asks for a prompt on a conexus tool never gets one), and leaves it
# answering on PreToolUse only. That is safe only if a PreToolUse allow does NOT override an
# `ask` rule, at user scope and at project scope. Records 26 (Bash, project scope), 51 and 52
# (a conexus MCP tool, both scopes, headless and interactive) measured it; this scenario keeps
# the measurement alive in the harness, beside 07 (the PermissionRequest side) and 28 (auto mode).
#
#   35a  user-scope ask rule ($TEST_HOME/.claude/settings.json, the same file as the hook)
#   35b  project-scope ask rule (<project>/.claude/settings.json; cwd is the project, not $TEST_HOME,
#        so the two scopes are two files)
#   35c  CONTROL: the same hook, no ask rule. The tool must run with no prompt: that proves the hook
#        fires, the allow is honoured and the server connects, so 35a and 35b's "no run" is the rule's.
#
# The answer for 35a and 35b is an OBSERVATION of this Claude Code build, compared with the answer
# recorded in docs/tables/cc-version-answers.toml (cc_ask_rule_verdict, runner.sh): "prompted" (the
# ask rule won; the baseline) agrees; "overridden" (the hook's allow ran the tool through the rule)
# FAILS "answer changed", and for this design that is a real finding, not a drift to record.
#
# The prompt text names the scope the rule came from ("from user settings", "from shared project
# settings"); the ladder requires it, so a rule read from the wrong file cannot pass as the right one.
# Launched with claude_start_enforced (no --dangerously-skip-permissions), defaultMode "default".
# Each leg keeps its evidence (hook log, stub log, pane tail, settings) and dismisses a pending prompt.

S35_PROJ="$TEST_HOME/s35proj"
S35_TOOL="mcp__stub__ping"

# The PreToolUse allow hook. $HOOK_LOG is baked in at write time (README "Hooks": PreToolUse
# command hooks do not inherit the harness env); only it expands now.
cat > "$TEST_HOME/.claude/pretooluse_allow_hook.sh" <<BASH_EOF
#!/usr/bin/env bash
INPUT=\$(cat)
echo "[\$(date +%s)] PRETOOLUSE_ALLOW_FIRED: \$INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"scenario-35-allow"}}))'
BASH_EOF
chmod +x "$TEST_HOME/.claude/pretooluse_allow_hook.sh"

cat > "$TEST_HOME/.mcp.json" <<EOF
{ "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
} }
EOF

# The project directory is its own trust root, pre-seeded the way _preseed_trust does for the
# harness's own paths (claude_start merges, it does not overwrite).
_s35_seed_project() {
    mkdir -p "$S35_PROJ/.claude"
    python3 - "$TEST_HOME/.claude.json" "$S35_PROJ" <<'PY'
import json, os, pathlib, sys
cfg_p, proj = pathlib.Path(sys.argv[1]), sys.argv[2]
try:
    data = json.loads(cfg_p.read_text())
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
projects = data.setdefault("projects", {})
for key in {proj, os.path.realpath(proj)}:
    entry = projects.setdefault(key, {})
    entry["hasTrustDialogAccepted"] = True
    entry["hasCompletedProjectOnboarding"] = True
cfg_p.write_text(json.dumps(data, indent=2))
PY
}

# _s35_write_settings <user-ask: 0|1> <project-ask: 0|1>: the user file always holds the hook; the
# ask rule goes to the user file, the project file, or neither.
_s35_write_settings() {
    local uask="$1" pask="$2" uperm='"allow": []' pperm=""
    [[ "$uask" == 1 ]] && uperm="\"ask\": [\"$S35_TOOL\"]"
    cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { $uperm, "defaultMode": "default" },
  "hooks": {
    "PreToolUse": [
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/pretooluse_allow_hook.sh" }] }
    ]
  }
}
EOF
    if [[ "$pask" == 1 ]]; then
        printf '{ "permissions": { "ask": ["%s"] } }\n' "$S35_TOOL" > "$S35_PROJ/.claude/settings.json"
    else
        printf '{}\n' > "$S35_PROJ/.claude/settings.json"
    fi
    cp "$S35_PROJ/.claude/settings.json" "$TEST_HOME/project-settings.json"
}

# _s35_settle: after the measured call, wait until the call's outcome is visible: a permission
# prompt on the pane, or the stub's log naming the tool. Up to 90 s; a model that never calls it
# leaves both absent and the ladder fails the leg as "nothing to conclude".
_s35_settle() {
    local deadline=$(( $(date +%s) + 90 ))
    while [[ $(date +%s) -lt $deadline ]]; do
        cc_permission_prompt_up && return 0
        [[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && return 0
        sleep 2
    done
    return 0
}

# _s35_leg <label> <user-ask> <project-ask>: one session. Sets S35_HOOK, S35_RAN, S35_PROMPT and
# S35_SCOPE (the scope the prompt names: user | project | none | other).
_s35_leg() {
    local label="$1" uask="$2" pask="$3"
    _s35_write_settings "$uask" "$pask"
    : > "$HOOK_LOG"
    : > "$STUB_LOG"
    cc_reset_transcripts
    send_keys "cd $S35_PROJ" Enter; sleep 0.3
    claude_start_enforced
    # Deferred-tool warmup (README "Deferred MCP tools"): load the schema before the measured call.
    claude_prompt "List your available tools whose name starts with mcp__, one per line. If none, reply NO-MCP-TOOLS."
    claude_wait 30
    claude_prompt "Call $S35_TOOL. Reply DONE."
    claude_wait 60
    _s35_settle

    S35_HOOK=0; [[ -s "$HOOK_LOG" ]] && grep -q "PRETOOLUSE_ALLOW_FIRED" "$HOOK_LOG" && S35_HOOK=1
    S35_RAN=0;  [[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && S35_RAN=1
    S35_PROMPT=0; cc_permission_prompt_up && S35_PROMPT=1
    S35_SCOPE=none
    if [[ $S35_PROMPT -eq 1 ]]; then
        # The dialog names where the rule came from; wrapped lines joined, whitespace collapsed.
        local text; text="$(capture -60 | tr '\n' ' ' | tr -s ' ')"
        if grep -qE "from user settings" <<<"$text"; then S35_SCOPE=user
        elif grep -qE "from (shared )?project settings" <<<"$text"; then S35_SCOPE=project
        else S35_SCOPE=other; fi
    fi
    cc_save_pane 60
    cc_keep_evidence "$label"
    cc_dismiss_permission_prompt
    claude_exit
    send_keys "cd $REPO_ROOT" Enter; sleep 0.3
}

_s35_seed_project

scenario "35a pretooluse_allow_vs_user_ask: PreToolUse allow hook only, user-scope ask rule for $S35_TOOL"
_s35_leg "35a-user-ask" 1 0
ha=$S35_HOOK; ra=$S35_RAN; pa=$S35_PROMPT; sa=$S35_SCOPE
echo "    35a: hook_fired=$ha tool_ran=$ra prompt_pending=$pa prompt_scope=$sa"

scenario "35b pretooluse_allow_vs_project_ask: PreToolUse allow hook only, project-scope ask rule for $S35_TOOL"
_s35_leg "35b-project-ask" 0 1
hb=$S35_HOOK; rb=$S35_RAN; pb=$S35_PROMPT; sb=$S35_SCOPE
echo "    35b: hook_fired=$hb tool_ran=$rb prompt_pending=$pb prompt_scope=$sb"

scenario "35c control: the same hook, no ask rule: the tool must run with no prompt"
_s35_leg "35c-control" 0 0
hc=$S35_HOOK; rc=$S35_RAN; pc=$S35_PROMPT
echo "    35c: hook_fired=$hc tool_ran=$rc prompt_pending=$pc"

cc_ask_rule_verdict "$ha" "$ra" "$pa" "$sa" "$hb" "$rb" "$pb" "$sb" "$hc" "$rc" "$pc"

rm -rf "$S35_PROJ" "$TEST_HOME/project-settings.json"
rm -f "$TEST_HOME/.mcp.json"
scenario_end
