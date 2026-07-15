#!/usr/bin/env bash
# Scenario 21 — RDR-184 gate item: which stop event fires for background
# teammates, and does {"decision":"block"} round-trip?
#
#   21a: SYNC control — plain Task dispatch. Expect SubagentStop fires in the
#        spawner session with agent-identifying payload.
#   21b: BACKGROUND teammate — Agent tool, run_in_background/named. Observe
#        whether SubagentStop (spawner side) and/or Stop (teammate's own
#        session) fires; discriminate via the teammate's unique final-message
#        marker inside the logged payloads.
#   21c: Block round-trip — Stop hook blocks ONCE (stop_hook_active guard)
#        until the final message contains a report marker; assert the session
#        continued and complied.
#
# Every leg is fail-closed: asserts the PRESENCE of expected markers.

STOP_LOG="$TEST_HOME/stop_events.log"

# ── 21a + 21b share one observation config: both hooks log raw stdin ─────────
scenario "21a sync control: SubagentStop fires for plain Task dispatch"

cat > "$TEST_HOME/.claude/log_stop_event.sh" <<BASH_EOF
#!/usr/bin/env bash
printf '%s %s\n' "\$1" "\$(cat)" >> "$STOP_LOG"
exit 0
BASH_EOF
chmod +x "$TEST_HOME/.claude/log_stop_event.sh"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent", "SendMessage"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStop": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/log_stop_event.sh SUBAGENT_STOP", "timeout": 10 }] }
    ],
    "Stop": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/log_stop_event.sh STOP", "timeout": 10 }] }
    ]
  }
}
EOF

: > "$STOP_LOG"
claude_start
claude_prompt "Use Task to dispatch the general-purpose agent. Description='21a probe'. Prompt: 'Reply with exactly SYNC-DONE-MARKER-QP44 and finish.' After it returns, reply DISPATCH-COMPLETE and stop."
claude_wait 120

if grep -q "^SUBAGENT_STOP " "$STOP_LOG" && grep "^SUBAGENT_STOP " "$STOP_LOG" | grep -q "SYNC-DONE-MARKER-QP44"; then
    pass "21a: SubagentStop fired for sync Task dispatch, payload carries the subagent's final message"
else
    fail "21a: no SubagentStop with the sync marker — log contents: $(head -c 400 "$STOP_LOG" 2>/dev/null)"
fi
# Payload field inventory (informational, printed into the run log)
grep -m1 "^SUBAGENT_STOP " "$STOP_LOG" | head -c 600 || true
claude_exit
scenario_end

# ── 21b: background teammate topology ────────────────────────────────────────
scenario "21b background teammate: which stop event fires on its idle"

: > "$STOP_LOG"
claude_start
claude_prompt "Use the Agent tool to spawn a background agent: subagent_type='general-purpose', name='probe21b', run_in_background=true, prompt='Reply with exactly BG-DONE-MARKER-ZZ91 and finish.' Immediately after spawning (do not wait for it), reply SPAWNED-OK and stop."
claude_wait 60
# give the background teammate time to run and idle, then poke the log
sleep 45
capture -100 >/dev/null 2>&1 || true

BG_SUBAGENT_STOP=0
BG_OWN_STOP=0
grep "^SUBAGENT_STOP " "$STOP_LOG" | grep -q "BG-DONE-MARKER-ZZ91" && BG_SUBAGENT_STOP=1
grep "^STOP " "$STOP_LOG" | grep -q "BG-DONE-MARKER-ZZ91" && BG_OWN_STOP=1

if [[ "$BG_SUBAGENT_STOP" == 1 && "$BG_OWN_STOP" == 1 ]]; then
    pass "21b: BOTH SubagentStop and own-session Stop fired for the background teammate"
elif [[ "$BG_SUBAGENT_STOP" == 1 ]]; then
    pass "21b: SubagentStop DID fire for the background teammate (docs caveat REFUTED); no own-session Stop with marker"
elif [[ "$BG_OWN_STOP" == 1 ]]; then
    pass "21b: only the teammate's OWN-SESSION Stop fired (docs caveat CONFIRMED — F1 moves to a Stop hook)"
else
    fail "21b: NEITHER event captured the teammate's marker — log: $(head -c 400 "$STOP_LOG" 2>/dev/null)"
fi
grep -E "^(SUBAGENT_)?STOP " "$STOP_LOG" | head -c 800 || true
claude_exit
scenario_end

# ── 21c: block round-trip with stop_hook_active guard ────────────────────────
scenario "21c block round-trip: Stop hook blocks once until report marker present"

cat > "$TEST_HOME/.claude/block_once.sh" <<'BASH_EOF'
#!/usr/bin/env bash
payload="$(cat)"
if printf '%s' "$payload" | grep -q '"stop_hook_active":true'; then
    exit 0   # already blocked once — let it stop (loop guard)
fi
if printf '%s' "$payload" | grep -q "REPORT-SENT-MARKER-XV77"; then
    exit 0   # report present — clean stop
fi
printf '{"decision": "block", "reason": "Before stopping, reply with exactly REPORT-SENT-MARKER-XV77 on its own line, then stop."}\n'
exit 0
BASH_EOF
chmod +x "$TEST_HOME/.claude/block_once.sh"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" },
  "hooks": {
    "Stop": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/block_once.sh", "timeout": 10 }] }
    ]
  }
}
EOF

claude_start
claude_prompt "Reply with exactly HELLO-ONLY-MARKER-JB03 and stop. Do not say anything else."
claude_wait 90

if capture -200 | grep -q "REPORT-SENT-MARKER-XV77"; then
    pass "21c: block round-trip works — session continued on decision:block and complied with the reason"
else
    fail "21c: no compliance marker after block — block round-trip did not work as documented"
fi
claude_exit
scenario_end
