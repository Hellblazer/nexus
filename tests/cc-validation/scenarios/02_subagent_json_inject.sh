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

CC_DEBUG_FILE="$TEST_HOME/cc-debug-02.log"; rm -f "$CC_DEBUG_FILE"
claude_start
# nexus-qsmc5: reply tokens in pieces, the marker's code absent from the prompt,
# and the verdict read from the SUBAGENT's own transcript (see scenario 01).
claude_prompt "Use the Task tool to dispatch the general-purpose agent. Description='subagent JSON inject probe'. Prompt for the subagent: 'Do not use any tools. Examine your context, apart from this message. Is the text SUB-JSON-MARKER- immediately followed by a four-character code (no space) anywhere in it? If yes, reply with the word FOUND immediately followed by -02S (no space), then a space, then that whole text including its code. If no, reply with the word NONE immediately followed by -02S (no space).'"
cc_wait_subagent_reply "FOUND-02S|NONE-02S" 180 || true
REPLIES02="$(cc_subagent_replies || true)"

if grep -qE "FOUND-02S.*SUB-JSON-MARKER-X9P2" <<<"$REPLIES02"; then
    pass "JSON additionalContext injected into subagent context"
else
    fail "JSON additionalContext did NOT inject — contradicts docs (the subagent did not report the marker line)"
    echo "$REPLIES02" | tail -5 | sed 's/^/    | /'
fi

cc_report_context_lines
cc_keep_evidence "02"
claude_exit
scenario_end
