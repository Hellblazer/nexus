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
# established that in auto mode the classifier can approve a harmless command
# on its own leniency with no hook involved at all, so tool_ran=1
# with no prompt is indistinguishable from either (a) the pre-fix bug
# (an explicit allow forced it, no classifier ever consulted) or (b) the
# classifier deciding on its own that this echo needs no confirmation. That is
# why the sessions run in `defaultMode: default` (trap 2 below), where this
# redirect write prompts unless a hook allowed it.
# Only a prompt reaching the pane proves the permission system was
# actually invoked for this command, which could not happen under the
# pre-fix behaviour (scenario 28's own finding: PreToolUse allow lands
# BEFORE the classifier and skips the prompt outright). So prompt_seen==1
# is the sole pass condition; tool_ran is recorded and, once the prompt is
# answered, used only as confirmatory diagnostic evidence that the gate is
# a real, functioning one and not stuck.
#
# THE REAL HOOKS' OWN STDOUT. Five early live variants, each installing a
# plugin whose hooks.json wired the routing scripts directly, all read
# `hook_fired=0`. Their readings do not stand in either direction: they used
# the hand-written installed_plugins.json route, and on Claude Code 2.1.302
# that route loads nothing ("Marketplace nexus-plugins not found", found
# 2026-10-17 with `claude plugin list`), so no hook of those plugins could have
# run there. The plugin is now installed through a local-directory marketplace
# and verified loaded (see cc_plugin_install in runner.sh). What is observable of the REAL
# hooks.json entries, and how:
#   - the session transcript records a hook's stdout and stderr only when one
#     of them is non-empty (the current plugin's guards print nothing on a
#     pass-through command, so it holds no PreToolUse:Bash record for them;
#     v7.64.1's guards print their `allow`, and it holds three);
#   - `claude --debug-file` logs every hook it runs, silent or not, as
#     `Hooks: PreToolUse:Bash [<command>] (plugin <p>) finished with status 0`.
#     _s32_count_real_hooks reads that for the two guards both plugins share;
#   - a marker hook appended to the SAME PreToolUse/Bash matcher group of the
#     installed copy (_s32_add_marker_hook; the plugin's scripts are untouched)
#     proves the group fires at all.
# A leg whose guards are not shown to have run is vacuous and fails (the
# 2026-10-17 review, T2 nexus/review-nexus-ueztp-969e07170-2026-10-17).
#
# Shadow hooks, kept as corroboration: settings.json registers hooks that
# re-invoke the same routing scripts of the plugin under test against the same
# stdin and log their raw stdout, so a regression that emits a decision is
# caught at the script level too. They are not what governs the call.
#
# Non-vacuity: the command below must be one neither guard's patterns match
# (no `git`, no `bd close/done`, no credential-shaped text), so this exercises
# the PASS-THROUGH path specifically. A green verdict also requires that the
# plugin loaded, that Claude Code is the pinned version, that the marker hook
# and BOTH real guards ran, and that the shadow hooks fired: each missing piece
# is a failure, because a green with any of them absent could not be told from
# "the guards were never reached at all". _s32_verdict is that ladder, with a
# truth-table test (tests/test_cc_validation_scenario32_control.py).

# POSITIVE CONTROL (nexus-ueztp). The run above proves the fix only if the
# harness can also be seen failing on the defect, and until this bead no run
# showed that (the critique of the advisory, T2
# nexus/critique-ghsa-mc84-advisory-draft-2026-09-28, named it). The scenario
# therefore runs twice, one session each: 32a installs the current plugin and
# must show the prompt; 32b installs the conexus plugin of git tag v7.64.1
# (the GHSA-mc84-6gjq-vm2p plugin), extracted from the tag with `git archive`
# and never hand-copied, and is DECLARED to fail the same check -- prompt
# skipped, command ran. 32b reports `pass` when it fails the check, `fail`
# when the prompt appears anyway. A control that shows the prompt means the
# real hooks.json entries do not govern the call in this harness (32a's pass
# is then vacuous) and must be reported as that, not tuned away. Both sessions
# are real and billed, so only this scenario runs the control; it needs
# Claude Code, which runs on chas in a container (tests/cc-validation/README.md
# "Running in a container").
#
# TWO HARNESS TRAPS THE FIRST CONTAINER RUNS UNCOVERED (2026-10-17, Claude Code
# 2.1.302, Haiku 5.5), both of which had made 32a's pass vacuous:
#   1. A hand-written installed_plugins.json entry does not load the plugin:
#      `claude plugin list` says "failed to load: Marketplace nexus-plugins not
#      found". With the plugin unloaded the control showed the prompt too. The
#      plugin is now installed through a local-directory marketplace and
#      verified loaded and enabled before a session starts.
#   2. In `defaultMode: auto` the classifier approves this harmless echo with
#      no hook involvement, so NEITHER plugin shows a prompt and the prompt
#      cannot tell them apart (32b then reports NOT INFORMATIVE). The sessions
#      therefore run in `defaultMode: default`, where a redirect write prompts.
#      A hook `allow` governs there too, as the control shows. CC_VAL_S32_MODE=auto
#      (run.sh --s32-mode auto) restores the old mode.
# The v7.64.1 session's transcript holds its REAL hooks.json entries emitting
# `permissionDecision: allow` (stdout recorded because it is non-empty); the
# current plugin's holds none. 32b requires those allow records.
#
# Custom launcher, copied from scenario 28 (must stand alone under
# `runner.sh --scenario 32`). Named for this scenario so it cannot be mistaken
# for scenario 28's or 16's claude_start_auto when several scenarios are
# sourced into one shell. Adds --debug-file, the only record of a SILENT hook.
S32_PERMISSION_MODE="${CC_VAL_S32_MODE:-default}"
S32_DEBUG_FILE=""
claude_start_s32() {
    _preseed_trust 2>/dev/null || true
    local _extra; _extra="$(_prepare_mcp_args 2>/dev/null || true)"
    send_keys "bash '$CLAUDE_FD_EXEC' --permission-mode=${S32_PERMISSION_MODE} --debug-file '${S32_DEBUG_FILE}' ${_extra}" Enter
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
        elif echo "$pane" | grep -qiE "Type a message|auto mode on|for shortcuts"; then
            break
        fi
        sleep 1
    done
    sleep 5
}

scenario "32 pretooluse_no_blanket_allow: unmatched Bash command, defaultMode=${S32_PERMISSION_MODE}, no permissions.allow rule -- (32a) the current conexus plugin must emit NO decision and the permission prompt must be reached; (32b) POSITIVE CONTROL, the v7.64.1 plugin (the GHSA-mc84-6gjq-vm2p blanket allow) must FAIL that same check"

# The tag the positive control installs. Pinned here and nowhere else in the
# harness; tests/test_cc_validation_scenario32_control.py reads it back.
S32_CONTROL_TAG="v7.64.1"
S32_CONTROL_PLUGIN_ROOT=""

# Fetch the control plugin tree from the git TAG, never a hand-copied tree.
# CC_VAL_CONTROL_PLUGIN_ROOT (set by the container entrypoint, which gets it
# from container/run.sh because the container has no git history) names an
# already-extracted `conexus/` directory; otherwise archive it from the
# checkout, fetching the tag first when the clone lacks it. Either way the
# tree's own plugin.json must say the pinned version, so a wrong or empty
# extraction cannot stand in for the vulnerable plugin.
_s32_stage_control_plugin() {
    local root="${CC_VAL_CONTROL_PLUGIN_ROOT:-}"
    if [[ -z "$root" ]]; then
        local dest="$TEST_HOME/control-plugin-${S32_CONTROL_TAG}"
        rm -rf "$dest"; mkdir -p "$dest"
        if ! git -C "$REPO_ROOT" rev-parse -q --verify "refs/tags/${S32_CONTROL_TAG}" >/dev/null 2>&1; then
            git -C "$REPO_ROOT" fetch -q origin "refs/tags/${S32_CONTROL_TAG}:refs/tags/${S32_CONTROL_TAG}" >/dev/null 2>&1 || true
        fi
        git -C "$REPO_ROOT" archive "${S32_CONTROL_TAG}" conexus 2>/dev/null | tar -x -C "$dest" 2>/dev/null || true
        root="$dest/conexus"
    fi
    local want="${S32_CONTROL_TAG#v}" got=""
    if [[ -f "$root/.claude-plugin/plugin.json" ]]; then
        got="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version",""))' "$root/.claude-plugin/plugin.json" 2>/dev/null || true)"
    fi
    if [[ "$got" != "$want" || ! -f "$root/hooks/hooks.json" || ! -f "$root/hooks/scripts/routing/credential_print_guard.py" ]]; then
        echo "    [32] control plugin tree at '$root' is not a ${S32_CONTROL_TAG} conexus plugin (plugin.json version='$got')" >&2
        return 1
    fi
    S32_CONTROL_PLUGIN_ROOT="$root"
}

# Append one marker-writing hook to the plugin's PreToolUse/Bash matcher group
# (the group its two routing guards sit in). The plugin's scripts and its other
# hooks.json entries are untouched; the marker proves the group fired.
_s32_add_marker_hook() {
    python3 - "$1" "$2" <<'PY'
import json, sys
path, marker = sys.argv[1], sys.argv[2]
doc = json.load(open(path))
groups = [g for g in doc["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"]
assert len(groups) == 1, "expected exactly one PreToolUse Bash matcher group"
groups[0]["hooks"].append({"type": "command", "command": "touch", "args": [marker]})
with open(path, "w") as f:
    json.dump(doc, f, indent=2)
PY
}

# Install <plugin_root> as the user-scope conexus plugin through the shared
# cc_plugin_install (runner.sh): a local-directory marketplace named
# nexus-plugins whose own hooks.json is what actually governs the Bash call
# below, verified loaded at the expected version. The plugin tree is copied byte
# for byte; the marker hook is appended to the copy's PreToolUse/Bash group
# (_s32_add_marker_hook, through the prepare callback).
#
# WHY NOT A HAND-WRITTEN installed_plugins.json: see runner.sh's plugin helpers
# and tests/cc-validation/README.md. This scenario's own did exactly that until
# nexus-ueztp, and its "the prompt was seen" held for the wrong reason.
#
# Sets S32_INSTALLED_ROOT (where Claude Code put the plugin) and returns non-zero
# unless `claude plugin list` reports the plugin enabled at the expected version.
S32_INSTALLED_ROOT=""
S32_MARKER_PATH=""
_s32_prepare_copy() {
    _s32_add_marker_hook "$1/hooks/hooks.json" "$S32_MARKER_PATH"
}
_s32_install_plugin() {
    local root="$1" marker="$3"
    S32_MARKER_PATH="$marker"
    cc_plugin_install "$root" conexus nexus-plugins _s32_prepare_copy || return 1
    S32_INSTALLED_ROOT="$CC_PLUGIN_INSTALLED_ROOT"
    [[ -f "$S32_INSTALLED_ROOT/hooks/hooks.json" ]]
}

# 0 when the running Claude Code is the pinned build: <want> is the version the
# image pins (container/Dockerfile ARG, handed over by run.sh), <got> the first
# word of `claude --version`. An unpinned run (<want> empty) is not a match.
_s32_version_ok() {
    [[ -n "${1:-}" && "${1:-}" == "${2:-}" ]]
}

# How many of the two routing guards both plugins carry show in Claude Code's
# debug log as having RUN under the installed conexus plugin (0, 1 or 2). The
# transcript cannot say: it omits a hook whose stdout and stderr are empty.
_s32_count_real_hooks() {
    local log="$1" n=0 s
    if [[ -f "$log" ]]; then
        for s in credential_print_guard.py subagent_git_write_requires_orchestrator.py; do
            if grep -Eq "Hooks: PreToolUse:Bash \[[^]]*/hooks/scripts/routing/${s}\] \(plugin conexus@nexus-plugins\) finished with status 0" "$log"; then
                n=$(( n + 1 ))
            fi
        done
    fi
    echo "$n"
}

# Count the PreToolUse:Bash hook records in the session transcripts under <dir>
# whose stdout is a permissionDecision: <shape> "any" counts every decision,
# "allow" only an allow. 32b needs the allow shape (the defect); a deny or ask
# is not the blanket allow.
_s32_transcript_decisions() {
    local dir="$1" shape="$2"
    find "$dir" -name '*.jsonl' -exec cat {} + 2>/dev/null | python3 -c '
import json, sys
shape = sys.argv[1]
n = 0
for line in sys.stdin:
    try:
        att = json.loads(line).get("attachment") or {}
    except ValueError:
        continue
    if att.get("hookName") != "PreToolUse:Bash":
        continue
    try:
        out = json.loads(att.get("stdout") or "")
    except ValueError:
        continue
    pd = (out.get("hookSpecificOutput") or {}).get("permissionDecision") if isinstance(out, dict) else None
    if pd is not None and (shape == "any" or pd == shape):
        n += 1
print(n)
' "$shape"
}

# The verdict ladder for one leg, as a function so a truth-table test can drive
# it. Arguments are key=value: leg=a|b, tag, mode, and the 0/1 (or count) inputs
# loaded version_ok marker real_hooks shadow prompt tool blanket real_dec
# a_prompt. Sets S32_VERDICT_KIND (pass|fail) and S32_VERDICT_MSG. Every
# "the guards never demonstrably ran" case is a fail, never a pass: the order
# puts those checks BEFORE any check of the prompt.
S32_VERDICT_KIND="" S32_VERDICT_MSG=""
_s32_verdict() {
    local leg="" tag="" mode="" kv who
    local loaded=0 version_ok=0 marker=0 real_hooks=0 shadow=0 prompt=0 tool=0 blanket=0 real_dec=0 a_prompt=0
    S32_VERDICT_KIND="fail"
    for kv in "$@"; do
        case "$kv" in
            leg=*) leg="${kv#leg=}" ;;
            tag=*) tag="${kv#tag=}" ;;
            mode=*) mode="${kv#mode=}" ;;
            loaded=*) loaded="${kv#loaded=}" ;;
            version_ok=*) version_ok="${kv#version_ok=}" ;;
            marker=*) marker="${kv#marker=}" ;;
            real_hooks=*) real_hooks="${kv#real_hooks=}" ;;
            shadow=*) shadow="${kv#shadow=}" ;;
            prompt=*) prompt="${kv#prompt=}" ;;
            tool=*) tool="${kv#tool=}" ;;
            blanket=*) blanket="${kv#blanket=}" ;;
            real_dec=*) real_dec="${kv#real_dec=}" ;;
            a_prompt=*) a_prompt="${kv#a_prompt=}" ;;
            *) S32_VERDICT_MSG="_s32_verdict: unknown argument '$kv'"; return 0 ;;
        esac
    done
    case "$leg" in
        a) who="32a" ;;
        b) who="32b control" ;;
        *) S32_VERDICT_MSG="_s32_verdict: leg must be a or b, got '$leg'"; return 0 ;;
    esac
    # Preconditions common to both legs: without all of them a verdict says
    # nothing about the defect.
    if [[ $loaded -eq 0 ]]; then
        S32_VERDICT_MSG="$who: the conexus plugin did not load in Claude Code (see the 'claude plugin list' output above) -- a plugin that never loaded emits no decision, so this run says nothing about the defect"
        return 0
    fi
    if [[ $version_ok -eq 0 ]]; then
        S32_VERDICT_MSG="$who: Claude Code is not the pinned build (container/Dockerfile ARG CLAUDE_CODE_VERSION; see claude-version.txt in the evidence directory) -- the measurement is of a build nobody pinned"
        return 0
    fi
    if [[ $marker -eq 0 ]]; then
        S32_VERDICT_MSG="$who: the marker hook appended to the plugin's PreToolUse/Bash matcher group never ran -- the plugin's Bash hooks did not fire in this session, so the prompt (or its absence) says nothing about them"
        return 0
    fi
    if [[ $real_hooks -lt 2 ]]; then
        S32_VERDICT_MSG="$who: only $real_hooks of the 2 real routing guards (credential_print_guard.py, subagent_git_write_requires_orchestrator.py) show as run under the plugin in the debug log -- the guards under test did not demonstrably run, so this run is vacuous (see debug.log in the evidence directory)"
        return 0
    fi
    if [[ $shadow -eq 0 ]]; then
        S32_VERDICT_MSG="$who: the shadow re-invocation of the real routing guards never fired -- the wiring itself is broken (check settings.json's PreToolUse/Bash matcher, or that the installed plugin's hooks/scripts/routing/*.py exist), so this run says nothing about the defect"
        return 0
    fi
    if [[ "$leg" == "a" ]]; then
        if [[ $blanket -eq 1 ]]; then
            S32_VERDICT_MSG="32a: REAL FINDING, do not mask: at least one real routing guard emitted an explicit permissionDecision for an UNMATCHED Bash command (see hook.log, or re-run with CC_VAL_DEBUG=1) -- this is nexus-452oy's defect: an explicit allow on a pass-through path bypasses Claude Code's own permission prompt and its auto-mode classifier for every conexus user"
        elif [[ $real_dec -gt 0 ]]; then
            S32_VERDICT_MSG="32a: REAL FINDING, do not mask: Claude Code recorded $real_dec PreToolUse:Bash hook output(s) carrying a permissionDecision from the REAL, installed plugin's own hooks.json entries (see the transcript in CC_VAL_EVIDENCE_DIR) -- an explicit decision on an UNMATCHED Bash command is nexus-452oy's defect"
        elif [[ $prompt -eq 0 ]]; then
            S32_VERDICT_MSG="32a: no permission prompt reached the pane (tool_ran=$tool) -- this is INDETERMINATE, not a pass: the command running silently is equally consistent with (a) the pre-fix bug (an explicit allow forcing it through) or (b) the permission mode approving a harmless echo with no hook involvement; only a reached prompt distinguishes the fix from the bug (see hook.log and the pane in the evidence directory)"
        else
            S32_VERDICT_KIND="pass"
            S32_VERDICT_MSG="32a: the PERMISSION PROMPT (mode $mode) reached the pane for this unmatched Bash command with the REAL conexus plugin loaded, its Bash matcher group firing (marker) and BOTH real routing guards shown running in the debug log -- scenario 28 already proved a PreToolUse allow governs and skips this prompt entirely, so reaching it here is direct proof the real plugin's hooks.json did not force one. The shadow re-invocation of the same routing scripts also emitted no permissionDecision, and the command completed after the prompt was answered (tool_ran=$tool, confirmatory). nexus-452oy fix confirmed live."
        fi
        return 0
    fi
    # leg b: the declared-to-fail positive control.
    if [[ $prompt -eq 1 ]]; then
        S32_VERDICT_MSG="32b control DID NOT FAIL 32a's check: the permission prompt reached the pane even with the ${tag} plugin (blanket_allow_seen=$blanket). REAL FINDING, do not adjust the scenario: either the real hooks.json entries do not govern the call in this harness, so 32a's pass cannot distinguish the fix from the bug, or ${tag} does not skip the prompt in this configuration. Keep the evidence (CC_VAL_EVIDENCE_DIR) and report."
    elif [[ $a_prompt -eq 0 ]]; then
        S32_VERDICT_MSG="32b control is NOT INFORMATIVE: it skipped the prompt (tool_ran=$tool), but so did the current plugin in 32a, so the two cannot be told apart in this configuration (permission mode '$mode': the classifier or the mode approves this command with no hook involvement). A control only demonstrates detection when the clean run shows the prompt and the vulnerable one does not."
    elif [[ $tool -eq 0 ]]; then
        S32_VERDICT_MSG="32b control: no prompt AND the command did not run -- INDETERMINATE (session stalled, auth or model failure), not a detection; see the pane in the evidence directory"
    elif [[ $blanket -eq 0 ]]; then
        S32_VERDICT_MSG="32b control: the prompt was skipped and the command ran, but the ${tag} routing scripts' own stdout carried no permissionDecision -- the skip is unexplained by the defect, so this is not the positive control it claims to be"
    elif [[ $real_dec -eq 0 ]]; then
        S32_VERDICT_MSG="32b control: the prompt was skipped and the command ran, but Claude Code recorded no PreToolUse:Bash hook output with permissionDecision \"allow\" from the ${tag} plugin's own hooks.json entries -- the skip is not shown to come from the plugin's hooks, so this is not yet a positive control (see the transcript in CC_VAL_EVIDENCE_DIR)"
    else
        S32_VERDICT_KIND="pass"
        S32_VERDICT_MSG="32b positive control: with the ${tag} plugin the harness SKIPPED the permission prompt (prompt_seen=0, tool_ran=1), Claude Code's own transcript records $real_dec PreToolUse:Bash outputs carrying permissionDecision \"allow\" from the plugin's REAL hooks.json entries, both real guards show as run in the debug log, and the shadow scripts' stdout agrees -- 32a's prompt_seen check demonstrably detects the GHSA-mc84-6gjq-vm2p defect, and the control failed that check as declared (nexus-ueztp)"
    fi
    return 0
}

# Shadow hooks: re-run the SAME routing script, from the plugin under test,
# against the same stdin, log its raw stdout. Corroboration only (see the
# header). NX_HOOK_PYTHON pins the interpreter these scripts' own
# `_interpreter.reexec_if_needed()` preamble resolves to, so the shadow
# run never depends on which python3 happens to be first on the sandboxed
# PATH.
_s32_write_shadow_hook() {
    local name="$1" plugin_root="$2" script_rel="$3"
    cat > "$TEST_HOME/.claude/shadow_${name}.sh" <<BASH_EOF
#!/usr/bin/env bash
INPUT=\$(cat)
echo "[\$(date +%s)] SHADOW_${name}_INVOKED" >> "$HOOK_LOG"
OUT=\$(NX_HOOK_PYTHON="\$(command -v python3)" python3 "${plugin_root}/${script_rel}" <<< "\$INPUT" 2>>"$HOOK_LOG")
echo "[\$(date +%s)] SHADOW_${name}_STDOUT: \$OUT" >> "$HOOK_LOG"
BASH_EOF
    chmod +x "$TEST_HOME/.claude/shadow_${name}.sh"
}

# Copy a run's evidence out of $TEST_HOME (which the runner's EXIT trap
# removes). CC_VAL_EVIDENCE_DIR is set by the container entrypoint to a mounted
# directory; unset, this does nothing. The debug log is kept only if it holds no
# credential-shaped string.
_s32_keep_evidence() {
    local label="$1" pane_text="$2"
    [[ -n "${CC_VAL_EVIDENCE_DIR:-}" ]] || return 0
    local d="${CC_VAL_EVIDENCE_DIR%/}/${label}"
    mkdir -p "$d"
    cp "$HOOK_LOG" "$d/hook.log" 2>/dev/null || true
    printf '%s\n' "$pane_text" > "$d/pane.txt"
    printf '%s\n' "${S32_CLAUDE_VERSION:-unknown}" > "$d/claude-version.txt"
    if [[ -f "$S32_DEBUG_FILE" ]] && ! grep -q 'sk-ant-' "$S32_DEBUG_FILE"; then
        cp "$S32_DEBUG_FILE" "$d/debug.log" 2>/dev/null || true
    fi
    cp "$TEST_HOME/.claude/plugins/installed_plugins.json" "$d/installed_plugins.json" 2>/dev/null || true
    cp "$TEST_HOME/plugin-list.txt" "$TEST_HOME/plugin-install.log" "$TEST_HOME/.claude/settings.json" "$d/" 2>/dev/null || true
    local t i=0
    while IFS= read -r t; do
        [[ -n "$t" ]] || continue
        i=$(( i + 1 ))
        cp "$t" "$d/transcript-$i.jsonl" 2>/dev/null || true
    done < <(find "$TEST_HOME/.claude/projects" -name '*.jsonl' 2>/dev/null)
}

# One session. Sets S32_PLUGIN_LOADED, S32_VERSION_OK, S32_MARKER_FIRED,
# S32_REAL_HOOKS, S32_SHADOW_FIRED, S32_PROMPT_SEEN, S32_TOOL_RAN,
# S32_BLANKET_ALLOW_SEEN, S32_TRANSCRIPT_HOOK_DECISIONS (any decision) and
# S32_TRANSCRIPT_ALLOWS (allow shape only).
_s32_run_session() {
    local label="$1" plugin_root="$2" version="$3"

    reset_scenario_state
    # "Yes, and don't ask again" would persist an allow rule into the project
    # settings and silence the NEXT session's prompt; answer once below and
    # clear any leftover rule anyway.
    rm -f "$TEST_HOME/.claude/settings.local.json"
    rm -rf "$TEST_HOME/.claude/projects"

    # The plugin install merges enabledPlugins/extraKnownMarketplaces into this
    # file, so it is written first.
    cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": [], "defaultMode": "$S32_PERMISSION_MODE" },
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

    local marker_hook="$TEST_HOME/s32-hook-marker-${label}"
    S32_DEBUG_FILE="$TEST_HOME/cc-debug-${label}.log"
    rm -f "$marker_hook" "$S32_DEBUG_FILE"
    S32_CLAUDE_VERSION="$(claude --version 2>/dev/null | awk 'NR==1{print $1}')"
    S32_VERSION_OK=0
    _s32_version_ok "${CC_VAL_EXPECTED_CLAUDE_VERSION:-}" "$S32_CLAUDE_VERSION" && S32_VERSION_OK=1

    # Non-vacuity: a plugin that did not load proves nothing in either
    # direction, so the session does not start without one.
    S32_PLUGIN_LOADED=0
    S32_MARKER_FIRED=0 S32_REAL_HOOKS=0
    S32_SHADOW_FIRED=0 S32_PROMPT_SEEN=0 S32_TOOL_RAN=0 S32_BLANKET_ALLOW_SEEN=0 S32_TRANSCRIPT_HOOK_DECISIONS=0 S32_TRANSCRIPT_ALLOWS=0
    if _s32_install_plugin "$plugin_root" "$label" "$marker_hook"; then
        S32_PLUGIN_LOADED=1
    else
        echo "    32/$label: the plugin did not load -- 'claude plugin list' said:" >&2
        sed 's/^/    | /' "$TEST_HOME/plugin-list.txt" "$TEST_HOME/plugin-install.log" >&2 2>/dev/null || true
        _s32_keep_evidence "$label" ""
        return 0
    fi
    _s32_write_shadow_hook GIT_WRITE "$S32_INSTALLED_ROOT" "hooks/scripts/routing/subagent_git_write_requires_orchestrator.py"
    _s32_write_shadow_hook CRED_GUARD "$S32_INSTALLED_ROOT" "hooks/scripts/routing/credential_print_guard.py"

    : > "$HOOK_LOG"
    local marker="$TEST_HOME/scenario32-${label}-marker.txt"
    rm -f "$marker"

    send_keys "cd $TEST_HOME" Enter; sleep 0.3
    claude_start_s32
    # A command neither guard's patterns match: no git, no bd close/done, no
    # credential-shaped text.
    claude_prompt "Run this exact Bash command and then reply DONE: echo scenario32-nexus-452oy-marker > $marker"
    claude_wait 60

    # The pass condition: a genuine permission PROMPT reaching the pane. See
    # the header comment for why this -- not tool_ran -- is what distinguishes
    # the fix from the bug.
    S32_PROMPT_SEEN=0
    local pane_text; pane_text="$(capture -200)"
    if grep -qE "Do you want to proceed\?" <<<"$pane_text"; then
        S32_PROMPT_SEEN=1
        echo "    [32/$label] permission prompt reached the pane -- answering 'yes, once' so the run also yields a confirmatory tool_ran signal"
        send_keys "1" Enter
        claude_wait 30
    fi

    S32_SHADOW_FIRED=0
    grep -q "SHADOW_.*_INVOKED" "$HOOK_LOG" 2>/dev/null && S32_SHADOW_FIRED=1

    # Whether the command completed. For the current plugin this is
    # confirmatory only; for the control it is half of the pass condition
    # (a prompt-less run that ALSO ran the command, not a stalled session).
    S32_TOOL_RAN=0
    [[ -f "$marker" ]] && grep -q "scenario32-nexus-452oy-marker" "$marker" 2>/dev/null && S32_TOOL_RAN=1

    # Direct evidence about the routing scripts' OWN content: none of their
    # stdout may carry an explicit permissionDecision for this pass-through
    # command. A bare no-decision shadow line reads as "SHADOW_<X>_STDOUT: "
    # (empty tail); grep for the literal defect shape instead of assuming
    # emptiness, so a regression that emits an "allow" (or ANY)
    # permissionDecision here is caught even if it also happens to include
    # some other trailing text.
    S32_BLANKET_ALLOW_SEEN=0
    grep -q '"permissionDecision"' "$HOOK_LOG" 2>/dev/null && S32_BLANKET_ALLOW_SEEN=1

    # The hook-fired signals: the marker file, and the two real guards in the
    # debug log. The transcript cannot show a silent hook (see the header).
    [[ -e "$marker_hook" ]] && S32_MARKER_FIRED=1
    S32_REAL_HOOKS="$(_s32_count_real_hooks "$S32_DEBUG_FILE")"

    # What the transcript recorded of the REAL hooks (non-empty output only).
    S32_TRANSCRIPT_HOOK_DECISIONS="$(_s32_transcript_decisions "$TEST_HOME/.claude/projects" any)"
    S32_TRANSCRIPT_ALLOWS="$(_s32_transcript_decisions "$TEST_HOME/.claude/projects" allow)"

    echo "    32/$label: plugin=$version claude=$S32_CLAUDE_VERSION version_ok=$S32_VERSION_OK marker_fired=$S32_MARKER_FIRED real_hooks_run=$S32_REAL_HOOKS/2 shadow_fired=$S32_SHADOW_FIRED  prompt_seen=$S32_PROMPT_SEEN  tool_ran=$S32_TOOL_RAN  blanket_allow_seen=$S32_BLANKET_ALLOW_SEEN  transcript_decisions=$S32_TRANSCRIPT_HOOK_DECISIONS transcript_allows=$S32_TRANSCRIPT_ALLOWS"
    if [[ "${CC_VAL_DEBUG:-0}" == "1" ]]; then
        echo "    -- hook.log --"
        sed 's/^/    | /' "$HOOK_LOG" 2>/dev/null || true
        echo "    -- pane (last 60 lines) --"
        capture -60 | sed 's/^/    | /' || true
    fi
    _s32_keep_evidence "$label" "$pane_text"

    claude_exit
    send_keys "cd $REPO_ROOT" Enter; sleep 0.3
}

# ─── 32a: the current plugin (the fix) ───────────────────────────────────────

_s32_run_session "32a-current" "$REPO_ROOT/conexus" "dev"
a_prompt_seen=$S32_PROMPT_SEEN
if [[ $S32_PROMPT_SEEN -eq 0 && $S32_PLUGIN_LOADED -eq 1 && "${CC_VAL_DEBUG:-0}" != "1" ]]; then
    echo "    -- hook.log (32a) --"
    sed 's/^/    | /' "$HOOK_LOG" 2>/dev/null || true
fi
_s32_verdict leg=a mode="$S32_PERMISSION_MODE" loaded=$S32_PLUGIN_LOADED version_ok=$S32_VERSION_OK \
    marker=$S32_MARKER_FIRED real_hooks=$S32_REAL_HOOKS shadow=$S32_SHADOW_FIRED prompt=$S32_PROMPT_SEEN \
    tool=$S32_TOOL_RAN blanket=$S32_BLANKET_ALLOW_SEEN real_dec=$S32_TRANSCRIPT_HOOK_DECISIONS
"$S32_VERDICT_KIND" "$S32_VERDICT_MSG"

# ─── 32b: POSITIVE CONTROL (nexus-ueztp) ─────────────────────────────────────
# Same harness, same command, same launcher, same settings -- only the plugin
# differs: the v7.64.1 conexus plugin, whose routing guards emit an explicit
# `allow` on every pass-through path (GHSA-mc84-6gjq-vm2p). 32a's pass condition
# is "a prompt reaches the pane"; that condition means something only if the
# harness can also be seen NOT reaching it when the old defect is present. This
# run declares the expectation inverted: the control must FAIL 32a's check
# (prompt skipped, command ran, the old scripts' own stdout carrying the
# allow). A control that unexpectedly shows the prompt is the important
# finding -- either the real hooks.json entries never govern in this harness
# (so 32a's pass proves nothing) or the defect is not what the advisory says.
# It is reported as a failure, never adjusted away.

if _s32_stage_control_plugin; then
    _s32_run_session "32b-control-${S32_CONTROL_TAG}" "$S32_CONTROL_PLUGIN_ROOT" "${S32_CONTROL_TAG#v}"
    _s32_verdict leg=b tag="$S32_CONTROL_TAG" mode="$S32_PERMISSION_MODE" loaded=$S32_PLUGIN_LOADED version_ok=$S32_VERSION_OK \
        marker=$S32_MARKER_FIRED real_hooks=$S32_REAL_HOOKS shadow=$S32_SHADOW_FIRED prompt=$S32_PROMPT_SEEN \
        tool=$S32_TOOL_RAN blanket=$S32_BLANKET_ALLOW_SEEN real_dec=$S32_TRANSCRIPT_ALLOWS a_prompt=$a_prompt_seen
    "$S32_VERDICT_KIND" "$S32_VERDICT_MSG"
else
    fail "32b control: the ${S32_CONTROL_TAG} plugin tree could not be staged from the git tag -- the positive control is declared, so its absence is a failure, never a skip"
fi

# Back to the empty plugin registry.
cc_plugin_uninstall_all

scenario_end
