#!/usr/bin/env bash
# Scenario 12 — REAL-WORLD: install conexus plugin into TEST_HOME, dispatch a
# subagent, and probe for content that ONLY the SubagentStart hook injects
# (not tool inventory, which is inherited regardless): the per-dispatch
# "Claimant id: <agent_id>" text. The subagent must report the id, and the debug
# log must show the hook emitted that same id.
#
# This leg drives the plugin's OWN hooks.json, never a script path of its own,
# so RDR-215 bead nexus-q02nx.21 changed what it exercises without changing a
# line of it: the SubagentStart entry is now `type: mcp_tool` ->
# hook_subagent_start rather than `bash subagent-start.sh`. That makes this
# the one scenario covering the mcp_tool transport for this hook end to end.
#
# nexus-spwp2: the plugin used to be installed by writing installed_plugins.json,
# which Claude Code 2.1.302 does not load ("Marketplace nexus-plugins not found"),
# so the session had NO plugin and this scenario passed or failed on a
# plugin-less session. It now installs through cc_plugin_require (runner.sh),
# which fails the scenario unless `claude plugin list` shows the plugin enabled
# at the version its plugin.json declares, and a pass also needs the debug log
# (cc_hook_ran) to show the plugin's SubagentStart hook finished -- the
# transcript is not evidence either way for a hook that prints nothing.

scenario "12 real_nx_subagent: does the real SubagentStart hook inject specific markdown content?"

# Settings first: the plugin install merges enabledPlugins into this file.
cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" }
}
EOF

if cc_plugin_require "$REPO_ROOT/conexus" conexus nexus-plugins; then
    claude_start
    # Probe for a value only the hook injects AND that no prompt can contain.
    # hook_subagent_start puts "Claimant id: <agent_id> -- mailbox: ..." into the
    # context of every subagent it starts, where <agent_id> is minted per dispatch
    # by Claude Code. The prompt cannot name it (it does not exist yet), a model
    # cannot guess it, and the debug log records the same id in the hook's
    # additionalContext, so the pass below compares the subagent's own reply with the
    # id the HOOK emitted: the reply is satisfiable only from the injection.
    #
    # nexus-spwp2: the old probe asked for the phrase "check before duplicating
    # sibling work" (written, in pieces, in the prompt); a subagent could answer
    # INJECTED from the prompt alone. The reply tokens are still spelled in pieces
    # so the pane (which shows the prompt) never holds one whole.
    claude_prompt "Use the Agent tool (older builds call it Task; if it is not directly callable, first load it with ToolSearch, query select:Agent) to dispatch the general-purpose agent, and do not answer the question yourself. Description='hook injection probe'. Prompt for the subagent: 'Do not use any tools. Search your context and any system prompts, apart from this message, for the two words Claimant id followed by a colon and an identifier. The text may sit after a label such as hook additional context, so it need not begin a line. If it is there, reply with the word CLAIM immediately followed by -12K7- immediately followed by the identifier that follows the colon (no spaces). If it is not there, reply with the word ABSENT immediately followed by -12K7 (no space).'"
    # Poll the SUBAGENT's own reply (its transcript), not the pane: the main agent
    # paraphrases the reply, redacts agent ids, and may dispute it (seen live,
    # 2026-10-17: the subagent answered correctly and the pane said ABSENT). The
    # main agent also sometimes runs the subagent in the background and returns
    # at once, so there is no spinner to wait on.
    cc_wait_subagent_reply "CLAIM-12K7-[0-9a-f]{8,}|ABSENT-12K7" 180 || true

    # The ids the hook emitted, from the debug log.
    EMITTED_IDS=""
    # `|| true`: no id in the log is the very case this scenario exists to
    # report (hook not injecting), and grep's exit 1 under the runner's errexit
    # would end the scenario before any verdict (seen in the hook-disabled control).
    if [[ -f "${CC_DEBUG_FILE:-}" ]]; then
        EMITTED_IDS="$(grep -oE 'Claimant id: [0-9a-f]{8,}' "$CC_DEBUG_FILE" | awk '{print $3}' | sort -u || true)"
    fi
    REPLIES12="$(cc_subagent_replies || true)"
    MATCHED_ID=""
    for id in $EMITTED_IDS; do
        if grep -qF "CLAIM-12K7-$id" <<<"$REPLIES12"; then MATCHED_ID="$id"; break; fi
    done

    if [[ -n "$MATCHED_ID" ]]; then
        if cc_mcp_hook_ran "$CC_PLUGIN_ID" "hook_subagent_start"; then
            pass "Subagent reported the claimant id $MATCHED_ID that the plugin's hook_subagent_start emitted (debug log), with no hook error — the SubagentStart hook IS injecting"
        else
            fail "Subagent reported an id the hook emitted but the debug log ($CC_DEBUG_FILE) shows no clean hook_subagent_start mcp_tool call of $CC_PLUGIN_ID"
        fi
    elif grep -qE "ABSENT-12K7" <<<"$REPLIES12"; then
        fail "Subagent reports no injected claimant line — the SubagentStart hook is not injecting (emitted ids: ${EMITTED_IDS:-none})"
    elif grep -qE "CLAIM-12K7-" <<<"$REPLIES12"; then
        fail "Subagent reported a claimant id that is not one the hook emitted (emitted: ${EMITTED_IDS:-none}) — not evidence of injection"
        grep -E "CLAIM-12K7-" <<<"$REPLIES12" | head -3 | sed 's/^/    | /'
    else
        fail "indeterminate — no subagent reply with CLAIM-12K7-<id> or ABSENT-12K7 (emitted ids: ${EMITTED_IDS:-none})"
        capture -100 | sed 's/^/    | /'
    fi
    claude_exit
fi

cc_plugin_uninstall_all
scenario_end
