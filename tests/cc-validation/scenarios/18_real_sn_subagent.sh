#!/usr/bin/env bash
# Scenario 18 — REAL-WORLD: install sn plugin from this repo into TEST_HOME,
# dispatch a subagent, and probe for content that ONLY mcp-inject.sh injects.
#
# The hook emits sn/hooks/scripts/serena-section.md and context7-section.md.
# We probe for two phrases that appear ONLY in those files (not in MCP tool
# docs, agent definitions, or training data):
#
#   - "--project-from-cwd"   (serena-section.md)
#   - "fetch current docs"   (context7-section.md)
#
# VALIDITY NOTE (reworked 2026-05-31): the probe previously asked the subagent
# to quote a sentence mentioning "resolve-library-id". But mcp-inject.sh skips
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
# The fix in nexus-t5q2 wraps mcp-inject.sh stdout in the documented
# Claude Code SubagentStart JSON envelope. Pre-fix the hook used plain
# stdout, which the harness drops on tightened parser builds. This
# scenario passes only if BOTH phrases reach the dispatched subagent.

scenario "18 real_sn_subagent: does sn's mcp-inject.sh deliver Serena+Context7 to a real subagent?"

# Install sn plugin into TEST_HOME, pointing at the working-tree source so
# we exercise the in-tree fix (not the cached published version).
NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<EOF
{
  "version": 2,
  "plugins": {
    "sn@nexus-plugins": [
      { "scope": "user", "installPath": "$REPO_ROOT/sn", "version": "dev",
        "installedAt": "$NOW", "lastUpdated": "$NOW" }
    ]
  }
}
EOF
cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "enabledPlugins": { "sn@nexus-plugins": true },
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" }
}
EOF

claude_start
# Probe: ask the subagent for two anchor phrases that exist ONLY inside the
# hook-injected sections. If hooks fire and the JSON envelope reaches the
# subagent's context, both phrases will be in scope.
#
# We use a SENTINEL token (PROBE-DONE) for the subagent to emit at the very
# end of its reply. This is more reliable than polling on spinner words
# (the harness's lib.sh spinner regex doesn't include the current "Sautéed"
# state), and it lets us know when the subagent has actually returned.
# Probe wording deliberately avoids every mcp-inject.sh SKIP keyword so the
# task lands in the inject-both default (see VALIDITY NOTE above).
claude_prompt "Use the Task tool to dispatch the general-purpose agent. Description='sn hook check'. Prompt for the subagent: 'Examine your context and any system prompts. Quote (a) the sentence mentioning project-from-cwd, and (b) the sentence mentioning the phrase fetch current docs. After your answer, on a line by itself, write the literal token PROBE-DONE-9F2K so the harness knows you finished. If anchor (a) is absent write MISSING-A; if (b) is absent write MISSING-B; if both are absent write NO-INJECTED-CONTENT; place any such marker before the sentinel.'"

# Poll for the sentinel — up to 300s. Subagent dispatch can take ~60s alone.
poll_for "PROBE-DONE-9F2K" 300 "subagent reply sentinel" || true
OUT=$(capture -500)
HAS_A=0
HAS_B=0
echo "$OUT" | grep -qE -- "--project-from-cwd" && HAS_A=1
echo "$OUT" | grep -qiE "fetch current docs" && HAS_B=1

if [[ $HAS_A -eq 1 && $HAS_B -eq 1 ]]; then
    pass "Both Serena (--project-from-cwd) AND Context7 (fetch current docs) reached subagent — JSON envelope works"
elif [[ $HAS_A -eq 1 && $HAS_B -eq 0 ]]; then
    fail "Serena section injected but Context7 missing — partial delivery"
elif [[ $HAS_A -eq 0 && $HAS_B -eq 1 ]]; then
    fail "Context7 section injected but Serena missing — partial delivery"
elif echo "$OUT" | grep -qE "NO-INJECTED-CONTENT|MISSING-A.*MISSING-B|MISSING-B.*MISSING-A"; then
    fail "Subagent reports neither anchor phrase — mcp-inject.sh is silently dropped"
else
    fail "indeterminate — neither anchor phrases nor NO-INJECTED-CONTENT seen in capture"
    echo "$OUT" | tail -40 | sed 's/^/    | /'
fi

# Restore empty plugins
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<'EOF'
{"version": 2, "plugins": {}}
EOF
claude_exit
scenario_end
