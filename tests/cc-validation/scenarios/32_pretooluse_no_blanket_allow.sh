#!/usr/bin/env bash
# Scenario 32 — nexus-452oy: does a Bash command NO routing rule matches
# still get auto-run WITHOUT Claude Code's own permission flow ever being
# consulted?
#
# Pre-fix, conexus's PreToolUse routing guards
# (subagent_git_write_requires_orchestrator.py, credential_print_guard.py;
# phase_review_close_requires_gate.py until its deletion at cleanup step A2) emitted an explicit
# `permissionDecision: allow` on every pass-through path -- "nothing to
# deny here" -- and scenario 28 already proved live that a PreToolUse
# `allow` GOVERNS in `defaultMode: auto`: it lands before the auto-mode
# classifier and short-circuits it, exactly like an explicit permission
# rule would. So every unmatched Bash command ran with ZERO further
# evaluation, for every conexus user, regardless of what the classifier
# would otherwise have decided. The fix (this bead) makes every
# pass-through/warn/fail-open path emit NO decision at all (empty stdout)
# instead, so an unmatched command defers to Claude Code's own permission
# flow exactly as if these hooks were not registered at all.
#
# PASS CONDITION (critic review round 2, e3238278a): a genuine permission
# PROMPT reaching the pane is the ONLY thing that distinguishes the fix
# from the bug. "the tool ran" alone does not -- scenario 28 already
# established that auto mode can approve a harmless command on the
# CLASSIFIER's own leniency with no hook involved at all, so tool_ran=1
# with no prompt is indistinguishable from either (a) the pre-fix bug
# (an explicit allow forced it, no classifier ever consulted) or (b) the
# classifier deciding on its own that this echo needs no confirmation.
# Only a prompt reaching the pane proves the permission system was
# actually invoked for this command, which could not happen under the
# pre-fix behaviour (scenario 28's own finding: PreToolUse allow lands
# BEFORE the classifier and skips the prompt outright). So prompt_seen==1
# is the sole pass condition; tool_ran is recorded and, once the prompt is
# answered, used only as confirmatory diagnostic evidence that the gate is
# a real, functioning one and not stuck.
#
# DIRECT OBSERVATION OF THE REAL HOOK'S STDOUT -- ATTEMPTED, NOT
# ACHIEVED (critic review round 2 asked for this instead of a shadow
# re-invocation; recording what was tried so the next attempt does not
# repeat it). Five live variants, each installing a plugin whose
# hooks.json wires the three real routing scripts directly (so their
# stdout is what governs the call, observable by teeing it to
# $HOOK_LOG), all produced `hook_fired=0` -- Claude Code never invoked
# any of them, although the SAME technique (a byte-identical, UNEDITED
# copy of the whole conexus plugin installed from a sibling tmp
# directory) demonstrably DOES get recognized and its hooks DO fire,
# proven with a throwaway scenario mirroring scenario 12's own
# SubagentStart content-injection probe against that copy. So the defect
# is specific to a REWRITTEN/PATCHED PreToolUse-Bash hooks.json entry,
# not to relocating or copying the plugin:
#   1. A minimal plugin directory (bare plugin.json + hooks/, no
#      agents/skills/commands/.mcp.json) -- hook_fired=0.
#   2. A full plugin copy with hooks.json REPLACED by a minimal
#      PreToolUse/Bash-only file, exec-form commands
#      ("command": "bash", "args": [...]) -- hook_fired=0.
#   3. Same, but the ORIGINAL acceptEdits/["Task"] permission mode
#      scenario 12 itself uses, ruling out `defaultMode: auto` as the
#      variable -- hook_fired=0.
#   4. Same, shell-form command (a single string, no "args" -- the shape
#      that worked when these wrappers were registered via settings.json
#      instead of a plugin, see below) -- hook_fired=0.
#   5. hooks.json left otherwise UNTOUCHED and only APPENDED to (the
#      existing, proven-loadable PreToolUse/Bash matcher group gets three
#      more entries), first with the wrapper's own resolved absolute
#      path, then with the literal `${CLAUDE_PLUGIN_ROOT}` token every
#      real entry in the file uses (on the hypothesis that the plugin
#      loader only trusts a hook command naming its own declared root
#      that way) -- hook_fired=0 both times.
# Every one of these five runs still reached a genuine permission prompt
# (prompt_seen=1), so Claude Code's own permission flow was engaged either
# way; what could not be confirmed live is that it was specifically the
# REAL routing scripts, invoked through their REAL hooks.json entries,
# that emitted no decision for this command. Given five ruled-out
# variables and no further hypothesis to test cheaply, this scenario
# falls back to the approach below instead of a sixth blind attempt.
#
# FALLBACK: a shadow hook that re-invokes the SAME real, unmodified,
# plugin-resident routing script against the SAME stdin Claude Code hands
# every PreToolUse hook, registered via settings.json's OWN "hooks" key
# ALONGSIDE (not instead of) the real conexus plugin, whose hooks.json is
# installed and runs unmodified from $REPO_ROOT/conexus (exactly as
# scenario 12 does). This is not what actually governs the tool call --
# the plugin's own hooks.json entries are -- so it does not by itself
# prove the REAL hook's decision; it is direct evidence about the real
# script's OWN CONTENT (the same three files, unmodified, plugin-resident,
# stdlib-only, byte-identical to what hooks.json invokes) reached from the
# same stdin shape, combined with the prompt_seen signal above (which does
# reflect what the real, installed plugin's own hooks.json actually
# decided) as the thing that distinguishes fix from bug.
#
# Non-vacuity: the command below must be one none of the three guards'
# patterns match (no `git`, no `bd close/done`, no credential-shaped
# text), so this exercises the PASS-THROUGH path specifically, and the
# shadow hook must actually fire -- an unfired shadow would make a green
# verdict indistinguishable from "the guards were never reached at all".

# Custom launcher, copied from scenario 28 (must stand alone under
# `runner.sh --scenario 32`).
claude_start_auto() {
    _preseed_trust 2>/dev/null || true
    local _extra; _extra="$(_prepare_mcp_args 2>/dev/null || true)"
    send_keys "bash '$CLAUDE_FD_EXEC' --permission-mode=auto ${_extra}" Enter
    sleep 8
    local deadline=$(( $(date +%s) + 60 ))
    local _trust_done=0
    while [[ $(date +%s) -lt $deadline ]]; do
        local pane; pane=$(capture)
        if [[ $_trust_done -eq 0 ]] && echo "$pane" | grep -qiE "trust this folder|project you trust"; then
            echo "    [auth] trust — accept"
            _tmux send-keys -t "${TMUX_SESSION}" Enter
            _trust_done=1; sleep 2
        elif echo "$pane" | grep -qiE "custom API key"; then
            _tmux send-keys -t "${TMUX_SESSION}" Enter; sleep 5
        elif echo "$pane" | grep -qiE "Type a message|auto mode on"; then
            break
        fi
        sleep 1
    done
    sleep 5
}

scenario "32 pretooluse_no_blanket_allow: unmatched Bash command, real conexus plugin installed, defaultMode=auto, no permissions.allow rule -- hook must emit NO decision and the permission prompt must be reached"

# Install the real conexus plugin, unmodified, exactly as scenario 12
# does -- its own hooks.json is what actually governs the Bash call below.
NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
mkdir -p "$TEST_HOME/.claude/plugins"
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<EOF
{
  "version": 2,
  "plugins": {
    "conexus@nexus-plugins": [
      { "scope": "user", "installPath": "$REPO_ROOT/conexus", "version": "dev",
        "installedAt": "$NOW", "lastUpdated": "$NOW" }
    ]
  }
}
EOF

# Shadow hooks: re-run the SAME real, plugin-resident routing script
# against the same stdin, log its raw stdout. See the header comment for
# why this is a fallback, not the originally-intended direct observation.
# NX_HOOK_PYTHON pins the interpreter these scripts' own
# `_interpreter.reexec_if_needed()` preamble resolves to, so the shadow
# run never depends on which python3 happens to be first on the sandboxed
# PATH.
_write_shadow_hook() {
    local name="$1" script_rel="$2"
    cat > "$TEST_HOME/.claude/shadow_${name}.sh" <<BASH_EOF
#!/usr/bin/env bash
INPUT=\$(cat)
echo "[\$(date +%s)] SHADOW_${name}_INVOKED" >> "$HOOK_LOG"
OUT=\$(NX_HOOK_PYTHON="\$(command -v python3)" python3 "$REPO_ROOT/conexus/${script_rel}" <<< "\$INPUT" 2>>"$HOOK_LOG")
echo "[\$(date +%s)] SHADOW_${name}_STDOUT: \$OUT" >> "$HOOK_LOG"
BASH_EOF
    chmod +x "$TEST_HOME/.claude/shadow_${name}.sh"
}
_write_shadow_hook GIT_WRITE "hooks/scripts/routing/subagent_git_write_requires_orchestrator.py"
_write_shadow_hook CRED_GUARD "hooks/scripts/routing/credential_print_guard.py"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "enabledPlugins": { "conexus@nexus-plugins": true },
  "permissions": { "allow": [], "defaultMode": "auto" },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "bash $TEST_HOME/.claude/shadow_GIT_WRITE.sh" },
          { "type": "command", "command": "bash $TEST_HOME/.claude/shadow_CRED_GUARD.sh" }
        ]
      }
    ]
  }
}
EOF

: > "$HOOK_LOG"
MARKER_FILE="$TEST_HOME/scenario32-marker.txt"
rm -f "$MARKER_FILE"

send_keys "cd $TEST_HOME" Enter; sleep 0.3
claude_start_auto
# A command none of the three guards' patterns match: no git, no bd
# close/done, no credential-shaped text.
claude_prompt "Run this exact Bash command and then reply DONE: echo scenario32-nexus-452oy-marker > $MARKER_FILE"
claude_wait 60

# The pass condition: a genuine permission PROMPT reaching the pane. See
# the header comment for why this -- not tool_ran -- is what distinguishes
# the fix from the bug.
prompt_seen=0
if capture | grep -qE "Do you want to proceed\?"; then
    prompt_seen=1
    echo "    [32] permission prompt reached the pane -- answering 'always allow' so the run also yields a confirmatory tool_ran signal"
    send_keys "2" Enter
    claude_wait 30
fi

shadow_fired=0
grep -q "SHADOW_.*_INVOKED" "$HOOK_LOG" 2>/dev/null && shadow_fired=1

# Confirmatory only (see header): whether the command completed once the
# prompt (if any) was answered. NOT part of the pass condition on its own.
tool_ran=0
[[ -f "$MARKER_FILE" ]] && grep -q "scenario32-nexus-452oy-marker" "$MARKER_FILE" 2>/dev/null && tool_ran=1

# Direct evidence about the real scripts' OWN content: none of their
# stdout may carry an explicit permissionDecision for this pass-through
# command. A bare no-decision shadow line reads as "SHADOW_<X>_STDOUT: "
# (empty tail); grep for the literal defect shape instead of assuming
# emptiness, so a regression that emits an "allow" (or ANY)
# permissionDecision here is caught even if it also happens to include
# some other trailing text.
blanket_allow_seen=0
grep -q '"permissionDecision"' "$HOOK_LOG" 2>/dev/null && blanket_allow_seen=1

echo "    32: shadow_fired=$shadow_fired  prompt_seen=$prompt_seen  tool_ran=$tool_ran  blanket_allow_seen=$blanket_allow_seen"
if [[ "${CC_VAL_DEBUG:-0}" == "1" || $prompt_seen -eq 0 ]]; then
    echo "    -- hook.log --"
    sed 's/^/    | /' "$HOOK_LOG" 2>/dev/null || true
    echo "    -- pane (last 60 lines) --"
    capture -60 | sed 's/^/    | /' || true
fi

claude_exit
send_keys "cd $REPO_ROOT" Enter; sleep 0.3

# Restore empty plugins (mirrors scenario 12's teardown).
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<'EOF'
{"version": 2, "plugins": {}}
EOF

if [[ $shadow_fired -eq 0 ]]; then
    fail "the shadow re-invocation of the three real routing guards never fired -- the wiring itself is broken, so this run says nothing about the defect (check settings.json's PreToolUse/Bash matcher, or that \$REPO_ROOT/conexus/hooks/scripts/routing/*.py still exist at these paths)"
elif [[ $blanket_allow_seen -eq 1 ]]; then
    fail "REAL FINDING, do not mask: at least one real routing guard emitted an explicit permissionDecision for an UNMATCHED Bash command (see hook.log above, or re-run with CC_VAL_DEBUG=1) -- this is nexus-452oy's defect: an explicit allow on a pass-through path bypasses Claude Code's own permission prompt and its auto-mode classifier for every conexus user"
elif [[ $prompt_seen -eq 0 ]]; then
    fail "no permission prompt reached the pane (tool_ran=$tool_ran) -- this is INDETERMINATE, not a pass: the command running silently is equally consistent with (a) the pre-fix bug (an explicit allow forcing it through) or (b) the auto-mode classifier approving a harmless echo on its own with no hook involvement, exactly as scenario 28 already showed it can. Only a reached prompt distinguishes the fix from the bug; see hook.log/pane above"
else
    pass "the auto-mode PERMISSION PROMPT reached the pane for this unmatched Bash command, under the REAL, installed conexus plugin -- scenario 28 already proved a PreToolUse allow governs and skips this prompt entirely, so reaching it here is direct proof the real plugin's own hooks.json did not force one. The shadow re-invocation of the same three real, unmodified routing scripts also confirms their own content emits no permissionDecision for this command, and it completed after the prompt was answered (tool_ran=$tool_ran, confirmatory). nexus-452oy fix confirmed live."
fi

scenario_end
