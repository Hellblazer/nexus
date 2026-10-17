#!/usr/bin/env bash
# Scenario 18 — REAL-WORLD: install sn plugin from this repo into TEST_HOME,
# dispatch a subagent, and probe for content that ONLY subagent_start.py injects.
#
# The hook emits sn/hooks/scripts/serena-section.md and context7-section.md.
# We probe for two phrases that appear ONLY in those files (not in MCP tool
# docs, agent definitions, or training data):
#
#   - "--project-from-cwd"   (serena-section.md)
#   - "fetch current docs"   (context7-section.md)
#
# VALIDITY NOTE (reworked 2026-05-31): the probe previously asked the subagent
# to quote a sentence mentioning "resolve-library-id". But subagent_start.py skips
# the Serena section when the agent task text contains "library" (a token-
# saving heuristic), and the word "library" inside "resolve-library-id" tripped
# it — so Serena was legitimately omitted and the test failed for a reason that
# had nothing to do with the JSON-envelope delivery under test. The Context7
# anchor is now "fetch current docs" (no heuristic trigger), and the probe
# wording avoids every SKIP keyword (library/framework/context7/package/
# dependency/migrate/refactor/research/...), so a neutral task lands in the
# inject-both default. The test now isolates exactly what it claims to test:
# does the JSON envelope deliver BOTH sections to a real subagent.
#
# The fix in nexus-t5q2 wraps the SubagentStart stdout in the documented
# Claude Code SubagentStart JSON envelope. Pre-fix the hook used plain
# stdout, which the harness drops on tightened parser builds. This
# scenario passes only if BOTH phrases reach the dispatched subagent.

scenario "18 real_sn_subagent: does sn's subagent_start.py deliver Serena+Context7 to a real subagent?"

# Install the sn plugin of THIS checkout (the in-tree fix, not a cached
# published version) the way Claude Code loads one: a local-directory marketplace
# (nexus-spwp2: a hand-written installed_plugins.json loads nothing on Claude
# Code 2.1.302, so this scenario ran with NO plugin). Settings are written first
# because the install merges enabledPlugins into them; cc_plugin_require FAILS
# the scenario unless `claude plugin list` shows sn enabled at the version its
# plugin.json declares.
cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" }
}
EOF

if cc_plugin_require "$REPO_ROOT/sn" sn nexus-plugins; then
    claude_start
    # Probe: ask the subagent for two anchor phrases that exist ONLY inside the
    # hook-injected sections. If hooks fire and the JSON envelope reaches the
    # subagent's context, both phrases will be in scope.
    #
    # We use a SENTINEL token (PROBE-DONE) for the subagent to emit at the very
    # end of its reply. This is more reliable than polling on spinner words
    # (the harness's lib.sh spinner regex doesn't include the current "Sautéed"
    # state), and it lets us know when the subagent has actually returned.
    # Probe wording deliberately avoids every subagent_start.py SKIP keyword so the task
    # lands in the inject-both default (see VALIDITY NOTE above). We ask for fixed
    # CONFIRMATION TOKENS rather than verbatim quotes: a binary "emit TOKEN if the
    # text is present" is far more robust than asking the model to reproduce a
    # sentence (which it paraphrases or omits, the prior false-fail). subagent_start.py
    # is independently verified to emit both sections for a neutral task via a direct
    # pipe test, so this scenario isolates the remaining question: does CC deliver
    # the SubagentStart JSON envelope into the subagent's context end-to-end.
    #
    # nexus-spwp2: every token below is spelled in the prompt as two pieces to be
    # joined ("SERENA" then "-OK-9F2K"), never whole. The pane shows the prompt
    # itself, so a whole token written into it matches immediately and this
    # scenario would pass (and its sentinel poll return) before the subagent
    # answered; only the reply contains the joined spelling.
    claude_prompt "Use the Task tool to dispatch the general-purpose agent. Description='sn hook check'. Prompt for the subagent: 'Look at the guidance injected into your context. On separate lines output: the word SERENA immediately followed by -OK-9F2K (no space) if your context contains the text project-from-cwd; the word DOCS immediately followed by -OK-9F2K (no space) if your context contains the text fetch current docs. If neither phrase is present, output the word NONE immediately followed by -OK-9F2K (no space). Then on a line by itself write the word PROBE immediately followed by -DONE-9F2K (no space).'"

    # Poll for the sentinel — up to 300s. Subagent dispatch can take ~60s alone.
    poll_for "PROBE-DONE-9F2K" 300 "subagent reply sentinel" || true
    OUT=$(capture -500)
    HAS_A=0
    HAS_B=0
    echo "$OUT" | grep -qE "SERENA-OK-9F2K" && HAS_A=1
    echo "$OUT" | grep -qE "DOCS-OK-9F2K" && HAS_B=1

    if [[ $HAS_A -eq 1 && $HAS_B -eq 1 ]]; then
        if cc_hook_ran "SubagentStart" "$CC_PLUGIN_ID"; then
            pass "Both Serena (--project-from-cwd) AND Context7 (fetch current docs) reached subagent and the debug log shows the plugin's SubagentStart hook finished — JSON envelope works"
        else
            fail "Both phrases reached the subagent but the debug log ($CC_DEBUG_FILE) shows no SubagentStart hook of $CC_PLUGIN_ID finishing — the content did not come from the plugin's hook"
        fi
    elif [[ $HAS_A -eq 1 && $HAS_B -eq 0 ]]; then
        fail "Serena section injected but Context7 missing — partial delivery"
    elif [[ $HAS_A -eq 0 && $HAS_B -eq 1 ]]; then
        fail "Context7 section injected but Serena missing — partial delivery"
    elif echo "$OUT" | grep -qE "NONE-OK-9F2K"; then
        fail "Subagent confirms NEITHER section in context — SubagentStart envelope not delivered end-to-end"
    else
        fail "indeterminate — no confirmation token (SERENA-OK/DOCS-OK/NONE-OK) seen in capture"
        echo "$OUT" | tail -40 | sed 's/^/    | /'
    fi

    claude_exit
fi

cc_plugin_uninstall_all
scenario_end
