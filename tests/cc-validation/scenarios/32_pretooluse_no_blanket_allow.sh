#!/usr/bin/env bash
# Scenario 32 — nexus-452oy: does a Bash command NO routing rule matches
# still get auto-run WITHOUT Claude Code's own permission flow ever being
# consulted?
#
# Pre-fix, conexus's PreToolUse routing guards
# (subagent_git_write_requires_orchestrator.py, phase_review_close_requires_
# gate.py, credential_print_guard.py) emitted an explicit
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
# REAL hooks, not synthetic: this installs the actual conexus plugin (like
# scenario 12) so its actual hooks.json PreToolUse Bash entries fire the
# actual routing scripts. A synthetic stand-in (like scenario 28's hand-
# written hook) would only prove the general Claude-Code mechanism, not
# that THIS bead's fix to THESE scripts landed.
#
# Observability problem and its fix: the real routing scripts' own
# telemetry (`log_routing_event`) posts to the engine's `routing_events`
# table over HTTP, best-effort -- there is no local JSONL a sandboxed
# TEST_HOME can read to prove a script fired or what it emitted. So this
# scenario adds three SHADOW hooks on the same PreToolUse Bash matcher,
# each re-invoking the SAME real script (subagent_git_write_requires_
# orchestrator.py / phase_review_close_requires_gate.py /
# credential_print_guard.py -- the three PLUGIN-resident, stdlib-only
# routing guards that need no `nx-hook`/CLI install, so they run
# unmodified inside a bare TEST_HOME) against the SAME stdin Claude Code
# hands every PreToolUse hook, and logs the script's raw stdout to
# $HOOK_LOG. This is a passive shadow: it changes nothing about what
# actually governs the tool call -- the plugin's OWN hooks.json entries
# (installed for real, run for real) are what does that -- it only makes
# the decision each one reached OBSERVABLE from outside the sandbox.
#
# Non-vacuity: the command below must be one none of the three guards'
# patterns match (no `git`, no `bd close/done`, no credential-shaped
# text), so this exercises the PASS-THROUGH path specifically, and the
# shadow hooks must actually fire -- an unfired shadow would make a green
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

scenario "32 pretooluse_no_blanket_allow: unmatched Bash command, real conexus routing hooks, defaultMode=auto, no permissions.allow rule -- hook must emit NO decision"

# Install the real conexus plugin (mirrors scenario 12).
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

# Three shadow hooks: re-run the SAME real, plugin-resident routing script
# against the same stdin, log its raw stdout, then discard it (the wrapper
# itself emits nothing -- the plugin's own hooks.json entry is what actually
# decides). NX_HOOK_PYTHON pins the interpreter these scripts' own
# `_interpreter.reexec_if_needed()` preamble resolves to, so the shadow run
# never depends on which python3 happens to be first on the sandboxed PATH.
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
_write_shadow_hook PHASE_REVIEW "hooks/scripts/routing/phase_review_close_requires_gate.py"
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
          { "type": "command", "command": "bash $TEST_HOME/.claude/shadow_PHASE_REVIEW.sh" },
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

# A genuine PERMISSION PROMPT reaching the pane is itself strong, direct
# evidence: scenario 28 already proved live that a PreToolUse
# `permissionDecision: allow` GOVERNS in `defaultMode: auto` and lands
# BEFORE the auto-mode classifier -- so under the pre-fix behaviour (every
# one of these three guards answering "allow" on this unmatched command)
# a prompt could never have reached the pane at all. Seeing one here means
# the permission system was genuinely consulted. Answer it (option 2,
# "always allow") so the run also produces the tool_ran signal below,
# rather than leaving the scenario's pass/fail hinging on prompt text
# alone.
prompt_seen=0
if capture | grep -qE "Do you want to proceed\?"; then
    prompt_seen=1
    echo "    [32] permission prompt reached the pane -- answering 'always allow' to let the run complete"
    send_keys "2" Enter
    claude_wait 30
fi

shadow_fired=0
grep -q "SHADOW_.*_INVOKED" "$HOOK_LOG" 2>/dev/null && shadow_fired=1

tool_ran=0
[[ -f "$MARKER_FILE" ]] && grep -q "scenario32-nexus-452oy-marker" "$MARKER_FILE" 2>/dev/null && tool_ran=1

# The core assertion: none of the three real guards' stdout may carry an
# explicit permissionDecision for this pass-through command. A bare
# no-decision shadow line reads as "SHADOW_<X>_STDOUT: " (empty tail);
# grep for the literal defect shape instead of assuming emptiness, so a
# regression that emits an "allow" (or ANY) permissionDecision here is
# caught even if it also happens to include some other trailing text.
blanket_allow_seen=0
grep -q '"permissionDecision"' "$HOOK_LOG" 2>/dev/null && blanket_allow_seen=1

echo "    32: shadow_fired=$shadow_fired  prompt_seen=$prompt_seen  tool_ran=$tool_ran  blanket_allow_seen=$blanket_allow_seen"
if [[ "${CC_VAL_DEBUG:-0}" == "1" || $tool_ran -eq 0 ]]; then
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
    fail "none of the three real routing guards fired for this Bash call -- the wiring itself is broken, so this run says nothing about the defect (check hooks.json PreToolUse/Bash matcher, or that $REPO_ROOT/conexus/hooks/scripts/routing/*.py still exist at these paths)"
elif [[ $blanket_allow_seen -eq 1 ]]; then
    fail "REAL FINDING, do not mask: at least one real routing guard emitted an explicit permissionDecision for an UNMATCHED Bash command (see hook.log above, or re-run with CC_VAL_DEBUG=1) -- this is nexus-452oy's defect: an explicit allow on a pass-through path bypasses Claude Code's own permission prompt and its auto-mode classifier for every conexus user"
elif [[ $tool_ran -eq 0 ]]; then
    fail "the Bash command never ran (no marker file written; prompt_seen=$prompt_seen) -- indeterminate: cannot tell whether a hook silently denied it, the model declined, or the harness itself stalled"
elif [[ $prompt_seen -eq 1 ]]; then
    pass "the auto-mode PERMISSION PROMPT reached the pane for this unmatched Bash command (proof the routing guards did NOT force an allow -- scenario 28 already showed a PreToolUse allow governs and skips this prompt entirely), all three real routing guards fired with no permissionDecision, and the command ran after being answered (nexus-452oy fix confirmed live)"
else
    pass "unmatched Bash command ran under defaultMode=auto with no permissions.allow rule, all three real routing guards fired (shadow_fired=1), and NONE emitted a permissionDecision -- the pass-through path defers to Claude Code's own permission flow instead of forcing an allow (nexus-452oy fix confirmed live)"
fi

scenario_end
