#!/usr/bin/env bash
# Scenario 27 — RDR-184 bead nexus-ccs9v.7: what does the SubagentStart hook
# payload carry, and does it discriminate (a) sync Task dispatch vs background
# teammate, (b) named vs unnamed agents?
#
# Mirrors scenario 21's topology harness. Every leg logs the FULL raw stdin of
# BOTH SubagentStart and SessionStart to the same log (a background teammate is
# a full session, so it may fire SessionStart rather than a spawner-side
# SubagentStart — logging both discriminates the two).
#
#   27a: SYNC control    — plain Task dispatch (in-process subagent).
#   27b: BACKGROUND NAMED — Agent tool, run_in_background=true, name=...
#   27c: BACKGROUND UNNAMED — Agent tool, run_in_background=true, no name.
#
# Each leg truncates the log first, then dumps whatever fired. This is a
# DETERMINATION probe (field inventory), not a strict fail-closed gate: the
# only hard assert is that 27a produces SOME start-event payload.

START_LOG="$TEST_HOME/start_events.log"

# ── Shared observation config: SubagentStart + SessionStart both log raw stdin ─
cat > "$TEST_HOME/.claude/log_start_event.sh" <<BASH_EOF
#!/usr/bin/env bash
printf '%s %s\n' "\$1" "\$(cat)" >> "$START_LOG"
exit 0
BASH_EOF
chmod +x "$TEST_HOME/.claude/log_start_event.sh"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent", "SendMessage"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/log_start_event.sh SUBAGENT_START", "timeout": 10 }] }
    ],
    "SessionStart": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/log_start_event.sh SESSION_START", "timeout": 10 }] }
    ]
  }
}
EOF

dump_log() {
    echo "    ----- START_LOG dump ($1) -----"
    if [[ -s "$START_LOG" ]]; then
        # One line per event; jq-pretty each payload if jq is present.
        while IFS= read -r line; do
            local tag="${line%% *}"
            local json="${line#* }"
            echo "    [$tag]"
            if command -v jq >/dev/null 2>&1; then
                printf '%s' "$json" | jq . 2>/dev/null | sed 's/^/      /' \
                    || { echo "      (raw, jq-parse-failed) $json"; }
            else
                echo "      $json"
            fi
        done < "$START_LOG"
    else
        echo "    (EMPTY — no start-event hook fired)"
    fi
    echo "    ----- end dump -----"
}

# ── 27a: sync Task dispatch ──────────────────────────────────────────────────
scenario "27a sync control: SubagentStart payload for plain Task dispatch"
: > "$START_LOG"
claude_start
claude_prompt "Use Task to dispatch the general-purpose agent. Description='start-probe-A'. Prompt: 'Reply with exactly A-SUB-DONE and finish.' After it returns, reply A-DISPATCH-COMPLETE and stop."
claude_wait 150

if grep -q "^SUBAGENT_START " "$START_LOG"; then
    pass "27a: SubagentStart fired for sync Task dispatch"
elif grep -q "^SESSION_START " "$START_LOG"; then
    fail "27a: only SessionStart fired for sync Task dispatch (unexpected — SubagentStart expected)"
else
    fail "27a: NO start-event hook fired for sync Task dispatch — $(head -c 200 "$START_LOG" 2>/dev/null)"
fi
dump_log 27a
claude_exit
scenario_end

# ── 27b: background NAMED teammate ───────────────────────────────────────────
scenario "27b background named: start payload for a named background teammate"
: > "$START_LOG"
claude_start
claude_prompt "Use the Agent tool to spawn a background agent: subagent_type='general-purpose', name='startprobeB', run_in_background=true, prompt='Reply with exactly B-SUB-DONE and finish.' Immediately after spawning (do NOT wait for it), reply B-SPAWNED-OK and stop."
claude_wait 90
sleep 30   # let the background teammate actually start and fire its start hook

echo "    27b events seen: SUBAGENT_START=$(grep -c '^SUBAGENT_START ' "$START_LOG" 2>/dev/null || echo 0) SESSION_START=$(grep -c '^SESSION_START ' "$START_LOG" 2>/dev/null || echo 0)"
if [[ -s "$START_LOG" ]]; then
    pass "27b: at least one start-event captured for the named background teammate"
else
    fail "27b: NO start-event hook fired for the named background teammate"
fi
# MORPHOLOGY TRIPWIRE (RDR-184 .7/.9 load-bearing): the Gap-1 consult rule
# keys on named agents arriving as agent_id="a<name>-<hash>" +
# agent_type=<name>. If a Claude Code update changes this encoding, the
# expectations-file guard silently stops matching — fail HERE, loudly.
if grep -q 'astartprobeB-' "$START_LOG"; then
    pass "27b: agent_id carries the named morphology (a<name>-<hash>)"
else
    fail "27b: agent_id named morphology MISSING — RDR-184 Gap-1 consult rule broken by payload change"
fi
if grep -q '"agent_type"[[:space:]]*:[[:space:]]*"startprobeB"' "$START_LOG"; then
    pass "27b: agent_type carries the dispatch name"
else
    fail "27b: agent_type != dispatch name — RDR-184 Gap-1 consult rule broken by payload change"
fi
dump_log 27b
claude_exit
scenario_end

# ── 27c: background UNNAMED dispatch ─────────────────────────────────────────
scenario "27c background unnamed: start payload for an unnamed background dispatch"
: > "$START_LOG"
claude_start
claude_prompt "Use the Agent tool to spawn a background agent WITHOUT a name: subagent_type='general-purpose', run_in_background=true, prompt='Reply with exactly C-SUB-DONE and finish.' Immediately after spawning (do NOT wait for it), reply C-SPAWNED-OK and stop."
claude_wait 90
sleep 30

echo "    27c events seen: SUBAGENT_START=$(grep -c '^SUBAGENT_START ' "$START_LOG" 2>/dev/null || echo 0) SESSION_START=$(grep -c '^SESSION_START ' "$START_LOG" 2>/dev/null || echo 0)"
if [[ -s "$START_LOG" ]]; then
    pass "27c: at least one start-event captured for the unnamed background dispatch"
else
    fail "27c: NO start-event hook fired for the unnamed background dispatch"
fi
dump_log 27c
claude_exit
scenario_end
