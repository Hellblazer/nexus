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
    # nexus-qsmc5: how many characters the hook must deliver for BOTH sections to
    # have arrived. The probe fragments below can be joined from the prompt, and
    # the reply tokens only say the model read the context it was given, so neither
    # separates "both sections delivered" from "some context delivered". The debug
    # log's `provided additionalContext (N chars)` does: N is the length of what the
    # plugin's hook handed Claude Code, so it must equal the two section files'
    # lengths (serena-section.md + context7-section.md), or those plus the worktree
    # section (a subagent dispatched from a linked worktree gets that one first).
    WHOLE18="$(python3 - "$REPO_ROOT/sn/hooks/scripts" <<'PY'
import pathlib, sys
d = pathlib.Path(sys.argv[1])
n = {name: len((d / f"{name}-section.md").read_text(encoding="utf-8")) for name in ("serena", "context7", "worktree")}
both = n["serena"] + n["context7"]
print(both, both + n["worktree"])
PY
)"
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
    # nexus-spwp2: nothing the reply is judged on appears whole in the prompt. The
    # pane shows the prompt itself and the subagent sees its own prompt, so a
    # phrase or token written whole into it is satisfiable without any injection
    # (the review of 9a96128b7 found the first fix still wrote the two probe
    # phrases whole). Each anchor phrase is given as two fragments the subagent
    # must join, and each reply token is two pieces to be joined. The hook's text
    # is fixed, so the pass also needs the debug log to show Claude Code accepted
    # non-empty additionalContext from the plugin's SubagentStart hook
    # (cc_hook_context_injected), and the subagent is told to use no tools so it
    # cannot read the plugin files off disk.
    claude_prompt "Use the Task tool to dispatch the general-purpose agent. Description='sn hook check'. Prompt for the subagent: 'Do not use any tools. Look at the guidance injected into your context, apart from this message. On separate lines output: the word SERENA immediately followed by -OK-9F2K (no space) if your context contains the text made by joining project-from and -cwd (no space); the word DOCS immediately followed by -OK-9F2K (no space) if your context contains the words fetch current followed by one space and the word docs. If neither is present, output the word NONE immediately followed by -OK-9F2K (no space). Then on a line by itself write the word PROBE immediately followed by -DONE-9F2K (no space).'"

    # Poll for the sentinel in the SUBAGENT's own reply (its transcript), up to
    # 300s; the pane is the main agent's paraphrase and may omit, add or dispute
    # a token (seen live, 2026-10-17, scenario 12).
    cc_wait_subagent_reply "PROBE-DONE-9F2K" 300 || true
    OUT=$(cc_subagent_replies)
    HAS_A=0
    HAS_B=0
    echo "$OUT" | grep -qE "SERENA-OK-9F2K" && HAS_A=1
    echo "$OUT" | grep -qE "DOCS-OK-9F2K" && HAS_B=1

    if [[ $HAS_A -eq 1 && $HAS_B -eq 1 ]]; then
        # shellcheck disable=SC2086 # WHOLE18 is a space-separated list of sizes
        if cc_hook_ran "SubagentStart" "$CC_PLUGIN_ID" && cc_hook_context_is_whole "SubagentStart" "subagent_start.py" "$CC_PLUGIN_ID" $WHOLE18; then
            pass "Both Serena (--project-from-cwd) AND Context7 (fetch current docs) reached subagent, the debug log shows the plugin's SubagentStart hook finished and Claude Code accepted exactly the two sections' text (${WHOLE18%% *} chars) — JSON envelope works"
        else
            fail "Both phrases reached the subagent but the debug log ($CC_DEBUG_FILE) does not show the SubagentStart hook of $CC_PLUGIN_ID finishing and delivering additionalContext of exactly the whole sections (want one of: $WHOLE18; delivered: $(cc_hook_context_chars "SubagentStart" "subagent_start.py" "$CC_PLUGIN_ID" | tr '\n' ' ' || true)) — the content was not delivered whole by the plugin's hook"
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
