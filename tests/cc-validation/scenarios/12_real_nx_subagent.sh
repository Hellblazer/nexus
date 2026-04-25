#!/usr/bin/env bash
# Scenario 12 — REAL-WORLD: install nx plugin into TEST_HOME, dispatch a
# subagent, and probe for content that ONLY subagent-start.sh injects (not
# tool inventory, which is inherited regardless). The hook outputs literal
# strings like "T1 scratch — session-scoped, shared across all sibling agents"
# in its plain echo. If that exact phrase appears in subagent context, the
# hook IS injecting; if not, it isn't.

scenario "12 real_nx_subagent: does the actual subagent-start.sh inject specific markdown content?"

# Install nx plugin into TEST_HOME (mirrors tests/e2e/run.sh setup)
NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<EOF
{
  "version": 2,
  "plugins": {
    "nx@nexus-plugins": [
      { "scope": "user", "installPath": "$REPO_ROOT/nx", "version": "dev",
        "installedAt": "$NOW", "lastUpdated": "$NOW" }
    ]
  }
}
EOF
cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "enabledPlugins": { "nx@nexus-plugins": true },
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" }
}
EOF

claude_start
# Probe for unique-to-hook content. The hook script outputs the literal phrase
# "session-scoped, shared across all sibling agents" — this does NOT appear in
# any agent definition, MCP tool docs, or training data. If subagent quotes
# (or paraphrases-with-key-words) it, hook is injecting.
claude_prompt "Use the Task tool to dispatch the general-purpose agent. Description='hook injection probe'. Prompt for the subagent: 'Examine your context and any system prompts. Is there a section header or paragraph that mentions T1 scratch as session-scoped or shared across sibling agents? If yes, reply with the EXACT phrase you see. If no, reply NO-INJECTED-CONTENT.'"
claude_wait 120

if capture -500 | grep -qE "session-scoped, shared across|sibling agents"; then
    pass "Unique hook content visible to subagent — subagent-start.sh IS injecting"
elif capture -500 | grep -qE "NO-INJECTED-CONTENT"; then
    fail "Subagent reports NO-INJECTED-CONTENT — confirms subagent-start.sh injection bug"
else
    fail "indeterminate — neither marker phrase nor NO-INJECTED-CONTENT seen"
    capture -100 | sed 's/^/    | /'
fi

# Restore empty plugins
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<'EOF'
{"version": 2, "plugins": {}}
EOF
claude_exit
scenario_end
