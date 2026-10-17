#!/usr/bin/env bash
# Scenario 12 — REAL-WORLD: install conexus plugin into TEST_HOME, dispatch a
# subagent, and probe for content that ONLY the SubagentStart hook injects
# (not tool inventory, which is inherited regardless). The hook emits literal
# strings like "<- check before duplicating sibling work". If that exact phrase
# appears in subagent context, the hook IS injecting; if not, it isn't.
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
    # Probe for content only the hook injects. hook_subagent_start puts the line
    # "T1 scratch  shared with siblings  <- check before duplicating sibling work"
    # into every subagent's context; that phrase is in no agent definition or MCP
    # tool doc.
    #
    # nexus-spwp2: the prompt and the reply tokens are spelled in pieces to be
    # joined, never whole. The pane shows the prompt itself, and the subagent sees
    # its own prompt: the old probe asked about "session-scoped ... sibling agents"
    # (a phrase the hook no longer emits), which the prompt itself contained, so
    # this scenario could pass whether or not anything was injected.
    claude_prompt "Use the Task tool to dispatch the general-purpose agent. Description='hook injection probe'. Prompt for the subagent: 'Examine your context and any system prompts, apart from this message. Does your context contain the words check before duplicating followed directly (one space) by the words sibling work? If yes, reply with the word INJECTED immediately followed by -12K7 (no space). If no, reply with the word ABSENT immediately followed by -12K7 (no space).'"
    # Poll for a reply token, not the spinner: the main agent sometimes runs the
    # subagent in the background and returns at once (seen live, 2026-10-17).
    poll_for "INJECTED-12K7|ABSENT-12K7" 180 "subagent reply token" || true

    if capture -500 | grep -qE "INJECTED-12K7"; then
        if cc_mcp_hook_ran "hook_subagent_start"; then
            pass "Subagent reports the hook's content and the debug log shows the plugin's hook_subagent_start call with no hook error — the SubagentStart hook IS injecting"
        else
            fail "Subagent reports the hook's content but the debug log ($CC_DEBUG_FILE) shows no clean hook_subagent_start mcp_tool call of $CC_PLUGIN_ID — the content did not come from the plugin's hook"
        fi
    elif capture -500 | grep -qE "ABSENT-12K7"; then
        fail "Subagent reports the hook's content is absent — the SubagentStart hook is not injecting"
    else
        fail "indeterminate — neither INJECTED-12K7 nor ABSENT-12K7 seen"
        capture -100 | sed 's/^/    | /'
    fi
    claude_exit
fi

cc_plugin_uninstall_all
scenario_end
