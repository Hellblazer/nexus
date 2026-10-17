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
# The control also reads the session transcript: Claude Code records each
# hook's real stdout there, so the v7.64.1 run shows its REAL hooks.json entries
# emitting `permissionDecision: allow` -- the direct observation five earlier
# attempts could not get -- and the current plugin shows none.
#
# Custom launcher, copied from scenario 28 (must stand alone under
# `runner.sh --scenario 32`).
S32_PERMISSION_MODE="${CC_VAL_S32_MODE:-default}"
claude_start_auto() {
    _preseed_trust 2>/dev/null || true
    local _extra; _extra="$(_prepare_mcp_args 2>/dev/null || true)"
    send_keys "bash '$CLAUDE_FD_EXEC' --permission-mode=${S32_PERMISSION_MODE} ${_extra}" Enter
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
# CC_VAL_CONTROL_PLUGIN_ROOT (set by run_in_container.sh, which stages the
# archive on the host because the container has no git history) names an
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

# Install <plugin_root> as the user-scope conexus plugin, unmodified, through a
# local-directory marketplace named nexus-plugins -- its own hooks.json is what
# actually governs the Bash call below. The plugin tree is copied byte for byte;
# the only synthesized file is the marketplace.json that names it.
#
# WHY NOT A HAND-WRITTEN installed_plugins.json (scenario 12's shape, and this
# scenario's own until nexus-ueztp): Claude Code 2.1.302 reads such an entry as
# "conexus@nexus-plugins ... Status: failed to load, Error: Marketplace
# nexus-plugins not found" and loads NOTHING from it -- no hooks, no agents. A
# plugin that never loaded emits no decision, so 32a's "the prompt was seen"
# held for the wrong reason, and it would hold just the same with the vulnerable
# plugin. Probably also why five earlier direct-observation attempts all read
# hook_fired=0. Found by running `claude plugin list` in the cc-val container.
#
# Sets S32_INSTALLED_ROOT (where Claude Code put the plugin) and returns non-zero
# unless `claude plugin list` reports the plugin enabled at the expected version.
S32_INSTALLED_ROOT=""
_s32_install_plugin() {
    local root="$1" label="$2" version mkt="$TEST_HOME/marketplace-$2"
    version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$root/.claude-plugin/plugin.json")"
    rm -rf "$mkt" "$TEST_HOME/.claude/plugins"
    mkdir -p "$mkt/.claude-plugin" "$TEST_HOME/.claude/plugins"
    cp -R "$root" "$mkt/conexus"
    cat > "$mkt/.claude-plugin/marketplace.json" <<EOF
{"name":"nexus-plugins","owner":{"name":"cc-validation"},"plugins":[{"name":"conexus","source":"./conexus","version":"$version"}]}
EOF
    HOME="$TEST_HOME" claude plugin marketplace add "$mkt" > "$TEST_HOME/plugin-install.log" 2>&1 || return 1
    HOME="$TEST_HOME" claude plugin install conexus@nexus-plugins --scope user >> "$TEST_HOME/plugin-install.log" 2>&1 || return 1
    HOME="$TEST_HOME" claude plugin list > "$TEST_HOME/plugin-list.txt" 2>&1 || return 1
    grep -q "Version: $version" "$TEST_HOME/plugin-list.txt" && grep -q "Status: ✔ enabled" "$TEST_HOME/plugin-list.txt" || return 1
    S32_INSTALLED_ROOT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugins"]["conexus@nexus-plugins"][0]["installPath"])' "$TEST_HOME/.claude/plugins/installed_plugins.json")"
    [[ -f "$S32_INSTALLED_ROOT/hooks/hooks.json" ]]
}

# Shadow hooks: re-run the SAME routing script, from the plugin under test,
# against the same stdin, log its raw stdout. See the header comment for why
# this is a fallback, not the originally-intended direct observation.
# NX_HOOK_PYTHON pins the interpreter these scripts' own
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
# removes). CC_VAL_EVIDENCE_DIR is set by run_in_container.sh to a mounted
# directory; unset, this does nothing.
_s32_keep_evidence() {
    local label="$1" pane_text="$2"
    [[ -n "${CC_VAL_EVIDENCE_DIR:-}" ]] || return 0
    local d="${CC_VAL_EVIDENCE_DIR%/}/${label}"
    mkdir -p "$d"
    cp "$HOOK_LOG" "$d/hook.log" 2>/dev/null || true
    printf '%s\n' "$pane_text" > "$d/pane.txt"
    cp "$TEST_HOME/.claude/plugins/installed_plugins.json" "$d/installed_plugins.json" 2>/dev/null || true
    cp "$TEST_HOME/plugin-list.txt" "$TEST_HOME/plugin-install.log" "$TEST_HOME/.claude/settings.json" "$d/" 2>/dev/null || true
    local t i=0
    while IFS= read -r t; do
        [[ -n "$t" ]] || continue
        i=$(( i + 1 ))
        cp "$t" "$d/transcript-$i.jsonl" 2>/dev/null || true
    done < <(find "$TEST_HOME/.claude/projects" -name '*.jsonl' 2>/dev/null)
}

# One session. Sets S32_SHADOW_FIRED, S32_PROMPT_SEEN, S32_TOOL_RAN,
# S32_BLANKET_ALLOW_SEEN, S32_TRANSCRIPT_HOOK_DECISIONS.
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

    # Non-vacuity: a plugin that did not load proves nothing in either
    # direction, so the session does not start without one.
    S32_PLUGIN_LOADED=0
    S32_SHADOW_FIRED=0 S32_PROMPT_SEEN=0 S32_TOOL_RAN=0 S32_BLANKET_ALLOW_SEEN=0 S32_TRANSCRIPT_HOOK_DECISIONS=0
    if _s32_install_plugin "$plugin_root" "$label"; then
        S32_PLUGIN_LOADED=1
    else
        echo "    32/$label: the plugin did not load -- 'claude plugin list' said:" >&2
        sed 's/^/    | /' "$TEST_HOME/plugin-list.txt" "$TEST_HOME/plugin-install.log" 2>/dev/null >&2 || true
        _s32_keep_evidence "$label" ""
        return 0
    fi
    _s32_write_shadow_hook GIT_WRITE "$S32_INSTALLED_ROOT" "hooks/scripts/routing/subagent_git_write_requires_orchestrator.py"
    _s32_write_shadow_hook CRED_GUARD "$S32_INSTALLED_ROOT" "hooks/scripts/routing/credential_print_guard.py"

    : > "$HOOK_LOG"
    local marker="$TEST_HOME/scenario32-${label}-marker.txt"
    rm -f "$marker"

    send_keys "cd $TEST_HOME" Enter; sleep 0.3
    claude_start_auto
    # A command none of the three guards' patterns match: no git, no bd
    # close/done, no credential-shaped text.
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

    # Diagnostic only: hook attachments Claude Code recorded in the session
    # transcript carrying a permissionDecision. This is the one channel that
    # shows what the REAL hooks.json entries decided (the header explains why
    # the direct-observation attempts failed); recorded, not asserted.
    S32_TRANSCRIPT_HOOK_DECISIONS=0
    local n
    n="$(find "$TEST_HOME/.claude/projects" -name '*.jsonl' -exec cat {} + 2>/dev/null | grep '"hookName":"PreToolUse:Bash"' | grep -c 'permissionDecision' || true)"
    S32_TRANSCRIPT_HOOK_DECISIONS="${n:-0}"

    echo "    32/$label: plugin=$version shadow_fired=$S32_SHADOW_FIRED  prompt_seen=$S32_PROMPT_SEEN  tool_ran=$S32_TOOL_RAN  blanket_allow_seen=$S32_BLANKET_ALLOW_SEEN  transcript_real_hook_decisions=$S32_TRANSCRIPT_HOOK_DECISIONS"
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
a_loaded=$S32_PLUGIN_LOADED a_shadow_fired=$S32_SHADOW_FIRED a_prompt_seen=$S32_PROMPT_SEEN a_tool_ran=$S32_TOOL_RAN
a_blanket=$S32_BLANKET_ALLOW_SEEN a_real=$S32_TRANSCRIPT_HOOK_DECISIONS
if [[ $a_prompt_seen -eq 0 && $a_loaded -eq 1 && "${CC_VAL_DEBUG:-0}" != "1" ]]; then
    echo "    -- hook.log (32a) --"
    sed 's/^/    | /' "$HOOK_LOG" 2>/dev/null || true
fi

if [[ $a_loaded -eq 0 ]]; then
    fail "32a: the current conexus plugin did not load in Claude Code (see the 'claude plugin list' output above) -- a plugin that never loaded emits no decision, so this run says nothing about the defect"
elif [[ $a_shadow_fired -eq 0 ]]; then
    fail "32a: the shadow re-invocation of the three real routing guards never fired -- the wiring itself is broken, so this run says nothing about the defect (check settings.json's PreToolUse/Bash matcher, or that \$REPO_ROOT/conexus/hooks/scripts/routing/*.py still exist at these paths)"
elif [[ $a_blanket -eq 1 ]]; then
    fail "32a: REAL FINDING, do not mask: at least one real routing guard emitted an explicit permissionDecision for an UNMATCHED Bash command (see hook.log above, or re-run with CC_VAL_DEBUG=1) -- this is nexus-452oy's defect: an explicit allow on a pass-through path bypasses Claude Code's own permission prompt and its auto-mode classifier for every conexus user"
elif [[ $a_real -gt 0 ]]; then
    fail "32a: REAL FINDING, do not mask: Claude Code recorded $a_real PreToolUse:Bash hook output(s) carrying a permissionDecision from the REAL, installed plugin's own hooks.json entries (see the transcript in CC_VAL_EVIDENCE_DIR) -- an explicit decision on an UNMATCHED Bash command is nexus-452oy's defect"
elif [[ $a_prompt_seen -eq 0 ]]; then
    fail "32a: no permission prompt reached the pane (tool_ran=$a_tool_ran) -- this is INDETERMINATE, not a pass: the command running silently is equally consistent with (a) the pre-fix bug (an explicit allow forcing it through) or (b) the auto-mode classifier approving a harmless echo on its own with no hook involvement, exactly as scenario 28 already showed it can. Only a reached prompt distinguishes the fix from the bug; see hook.log/pane above"
else
    pass "32a: the PERMISSION PROMPT (mode $S32_PERMISSION_MODE) reached the pane for this unmatched Bash command, under the REAL, installed conexus plugin -- scenario 28 already proved a PreToolUse allow governs and skips this prompt entirely, so reaching it here is direct proof the real plugin's own hooks.json did not force one. The shadow re-invocation of the same real, unmodified routing scripts also confirms their own content emits no permissionDecision for this command, and it completed after the prompt was answered (tool_ran=$a_tool_ran, confirmatory). nexus-452oy fix confirmed live."
fi

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
    b_loaded=$S32_PLUGIN_LOADED b_shadow_fired=$S32_SHADOW_FIRED b_prompt_seen=$S32_PROMPT_SEEN b_tool_ran=$S32_TOOL_RAN
    b_blanket=$S32_BLANKET_ALLOW_SEEN b_real=$S32_TRANSCRIPT_HOOK_DECISIONS
    if [[ $b_loaded -eq 0 ]]; then
        fail "32b control: the ${S32_CONTROL_TAG} plugin did not load in Claude Code (see the 'claude plugin list' output above), so this control run says nothing"
    elif [[ $b_shadow_fired -eq 0 ]]; then
        fail "32b control: the shadow re-invocation never fired -- the wiring is broken, so this control run says nothing"
    elif [[ $b_prompt_seen -eq 1 ]]; then
        fail "32b control DID NOT FAIL 32a's check: the permission prompt reached the pane even with the ${S32_CONTROL_TAG} plugin (blanket_allow_seen=$b_blanket). REAL FINDING, do not adjust the scenario: either the real hooks.json entries do not govern the call in this harness, so 32a's pass cannot distinguish the fix from the bug, or ${S32_CONTROL_TAG} does not skip the prompt in this configuration. Keep the evidence (CC_VAL_EVIDENCE_DIR) and report."
    elif [[ $a_prompt_seen -eq 0 ]]; then
        fail "32b control is NOT INFORMATIVE: it skipped the prompt (tool_ran=$b_tool_ran), but so did the current plugin in 32a, so the two cannot be told apart in this configuration (permission mode '$S32_PERMISSION_MODE': the classifier or the mode approves this command with no hook involvement). A control only demonstrates detection when the clean run shows the prompt and the vulnerable one does not."
    elif [[ $b_tool_ran -eq 0 ]]; then
        fail "32b control: no prompt AND the command did not run -- INDETERMINATE (session stalled, auth or model failure), not a detection; see the pane in the evidence directory"
    elif [[ $b_blanket -eq 0 ]]; then
        fail "32b control: the prompt was skipped and the command ran, but the ${S32_CONTROL_TAG} routing scripts' own stdout carried no permissionDecision -- the skip is unexplained by the defect, so this is not the positive control it claims to be"
    elif [[ $b_real -eq 0 ]]; then
        fail "32b control: the prompt was skipped and the command ran, but Claude Code recorded no PreToolUse:Bash hook output with a permissionDecision from the ${S32_CONTROL_TAG} plugin's own hooks.json entries -- the skip is not shown to come from the plugin's hooks, so this is not yet a positive control (see the transcript in CC_VAL_EVIDENCE_DIR)"
    else
        pass "32b positive control: with the ${S32_CONTROL_TAG} plugin the harness SKIPPED the permission prompt (prompt_seen=0, tool_ran=1), Claude Code's own transcript records $b_real PreToolUse:Bash outputs carrying permissionDecision from the plugin's REAL hooks.json entries, and the shadow scripts' stdout agrees -- 32a's prompt_seen check demonstrably detects the GHSA-mc84-6gjq-vm2p defect, and the control failed that check as declared (nexus-ueztp)"
    fi
else
    fail "32b control: the ${S32_CONTROL_TAG} plugin tree could not be staged from the git tag -- the positive control is declared, so its absence is a failure, never a skip"
fi

# Restore empty plugins (mirrors scenario 12's teardown).
rm -rf "$TEST_HOME/.claude/plugins"; mkdir -p "$TEST_HOME/.claude/plugins"
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<'EOF'
{"version": 2, "plugins": {}}
EOF

scenario_end
