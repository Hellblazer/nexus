#!/usr/bin/env bash
# Scenario 02 — SubagentStart with JSON additionalContext should inject in interactive.

scenario "02 subagent_json_inject: additionalContext JSON should inject"

cat > "$TEST_HOME/.claude/hook_inject.py" <<'EOF'
#!/usr/bin/env python3
import json, sys
try: payload = json.loads(sys.stdin.read())
except Exception: payload = {}
out = {"hookSpecificOutput": {"hookEventName": "SubagentStart",
       "additionalContext": "=== INJECTED ===\nMarker: SUB-JSON-MARKER-X9P2\n=== END ==="}}
print(json.dumps(out))
EOF
chmod +x "$TEST_HOME/.claude/hook_inject.py"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "",
        "hooks": [{ "type": "command",
                    "command": "python3 $TEST_HOME/.claude/hook_inject.py" }] }
    ]
  }
}
EOF

claude_start
claude_prompt "Use the Task tool to dispatch the general-purpose agent. Description='subagent JSON inject probe'. Prompt: 'Examine your context. Quote any line beginning with SUB-JSON-MARKER- exactly. If none, reply NO-MARKER.'"
claude_wait 90

if capture -300 | grep -qE "SUB-JSON-MARKER-X9P2"; then
    pass "JSON additionalContext injected into subagent context"
else
    fail "JSON additionalContext did NOT inject — contradicts docs"
    capture -50 | sed 's/^/    | /'
fi

claude_exit
scenario_end
