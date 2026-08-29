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
# only hard assert is that 27a produces SOME start-event payload — EXCEPT in
# 27b, whose payload-shape asserts are a deliberate hard tripwire (below).
#
# PAYLOAD SHAPE RE-MEASURED AT CC 2.1.251 (bead nexus-houpu, 2026-08-29).
# The encoding this scenario used to pin is GONE. A NAMED background
# teammate now arrives exactly like an unnamed one:
#     agent_id   == an OPAQUE "a<hex>" handle (e.g. "aeb1c1b56623244ae")
#     agent_type == the dispatch's subagent_type, verbatim
# and the dispatch NAME appears in no field of the payload. The old
# "a<name>-<hash>" agent_id / "agent_type == <name>" asserts pinned an
# encoding Claude Code no longer emits, so they are replaced by asserts on
# the shape it DOES emit, plus a positive check that the ledger still pairs
# the dispatch by TYPE — which is what the RDR-184 Gap-1 guard keys on
# (expectations_owes_report, and since nexus-houpu the audit surfaces too).

START_LOG="$TEST_HOME/start_events.log"
SID_FILE="$TEST_HOME/orchestrator_session_id"
LEDGER_STATE="$TEST_HOME/.local/state"

# ── Shared observation config: SubagentStart + SessionStart both log raw stdin ─
cat > "$TEST_HOME/.claude/log_start_event.sh" <<BASH_EOF
#!/usr/bin/env bash
printf '%s %s\n' "\$1" "\$(cat)" >> "$START_LOG"
exit 0
BASH_EOF
chmod +x "$TEST_HOME/.claude/log_start_event.sh"

# ── SessionStart SID capture: the ledger is per-session-id, and the census
# assert in 27b needs the ORCHESTRATOR's id. First write wins — a background
# teammate is itself a full session and fires its own SessionStart, which
# must not overwrite the spawner's id. STDOUT-SILENT: SessionStart stdout is
# injected as context into the session.
cat > "$TEST_HOME/.claude/capture_sid.sh" <<BASH_EOF
#!/usr/bin/env bash
payload="\$(cat)"
[[ -s "$SID_FILE" ]] && exit 0
printf '%s' "\$payload" | "$REPO_ROOT/.venv/bin/python" -c '
import json, sys
try:
    print(json.load(sys.stdin).get("session_id") or "", end="")
except Exception:
    pass
' > "$SID_FILE" 2>/dev/null
exit 0
BASH_EOF
chmod +x "$TEST_HOME/.claude/capture_sid.sh"

# The two real ledger hooks are wired here, not just the logging one: 27b's
# recognition assert is about what the SHIPPED hooks record, and a scenario
# that reimplemented their write path would prove nothing about them.
# XDG_STATE_HOME is pinned explicitly so the ledger lands somewhere the
# assert below can read without depending on the hook's inherited HOME.
DISPATCH_EXPECT_HOOK="env XDG_STATE_HOME=$LEDGER_STATE bash $REPO_ROOT/conexus/hooks/scripts/agent-dispatch-expect.sh"
START_STAMP_HOOK="env XDG_STATE_HOME=$LEDGER_STATE bash $REPO_ROOT/conexus/hooks/scripts/subagent-start-stamp.sh"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent", "SendMessage"], "defaultMode": "acceptEdits" },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Agent|Task",
        "hooks": [{ "type": "command", "command": "$DISPATCH_EXPECT_HOOK", "timeout": 10 }] }
    ],
    "SubagentStart": [
      { "matcher": "",
        "hooks": [
          { "type": "command", "command": "bash $TEST_HOME/.claude/log_start_event.sh SUBAGENT_START", "timeout": 10 },
          { "type": "command", "command": "$START_STAMP_HOOK", "timeout": 10 }
        ] }
    ],
    "SessionStart": [
      { "matcher": "",
        "hooks": [
          { "type": "command", "command": "bash $TEST_HOME/.claude/log_start_event.sh SESSION_START", "timeout": 10 },
          { "type": "command", "command": "bash $TEST_HOME/.claude/capture_sid.sh", "timeout": 10 }
        ] }
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
rm -f "$SID_FILE"
rm -rf "$LEDGER_STATE/nexus/orchestration"
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
# PAYLOAD-SHAPE TRIPWIRE (RDR-184 .7/.9 load-bearing, re-aimed at CC 2.1.251
# by nexus-houpu). The Gap-1 guard pairs a dispatch to its declaration by
# agent_type, because that is the only value BOTH sides of the ledger carry.
# These asserts pin the two halves of that: agent_type MUST equal the
# dispatch's subagent_type, and the agent_id MUST NOT be relied on to carry
# anything (it is opaque). If a future Claude Code release changes either,
# fail HERE, loudly, rather than letting the guard silently stop matching.
if grep -qE '"agent_id"[[:space:]]*:[[:space:]]*"a[0-9a-f]+"' "$START_LOG"; then
    pass "27b: agent_id is the opaque a<hex> handle"
else
    fail "27b: agent_id is not the opaque a<hex> shape — payload changed, re-verify the ledger's keying"
fi
if grep -q 'astartprobeB-' "$START_LOG"; then
    fail "27b: agent_id carries an 'a<name>-' prefix again — the retired morphology is BACK; re-open nexus-houpu before trusting the audit surfaces"
else
    pass "27b: agent_id carries NO 'a<name>-' prefix (the retired encoding stays retired)"
fi
if grep -q '"agent_type"[[:space:]]*:[[:space:]]*"general-purpose"' "$START_LOG"; then
    pass "27b: agent_type carries the subagent_type verbatim (the ledger's only key)"
else
    fail "27b: agent_type != subagent_type — RDR-184 Gap-1 keying broken by payload change"
fi
# The dispatch NAME must be absent from the payload entirely. This is the
# assert that makes "you cannot key on the name" a measured fact rather than
# an inference from the agent_id alone.
if grep -q 'startprobeB' "$START_LOG"; then
    fail "27b: the dispatch name 'startprobeB' IS observable in the start payload — a name key is available again; re-open nexus-houpu"
else
    pass "27b: the dispatch name is absent from every start-event field"
fi
# END-TO-END: the SHIPPED hooks (PreToolUse dispatch-expect + SubagentStart
# stamp, both wired in this scenario's settings.json) must produce a ledger
# in which this dispatch is recognised BY TYPE and reads as declared. This
# is the half the payload asserts above cannot cover: that the two hooks
# agree on the key across the PreToolUse/SubagentStart boundary.
B_SID="$(cat "$SID_FILE" 2>/dev/null || true)"
if [[ -z "$B_SID" ]]; then
    fail "27b: no orchestrator session_id captured — cannot audit the ledger"
else
    census_out="$(
        XDG_STATE_HOME="$LEDGER_STATE" bash -c \
            "source '$REPO_ROOT/tests/e2e/lib/expectations.sh'; expectations_census '$B_SID'" \
            2>/dev/null || true
    )"
    if grep -q 'BLINDSPOT	checked=1 recognized=1 unrecognized=0' <<<"$census_out"; then
        pass "27b: ledger census recognises the named background dispatch by type (recognized=1)"
    else
        fail "27b: ledger census did not recognise the dispatch — $(grep BLINDSPOT <<<"$census_out" || echo '(no BLINDSPOT line)')"
    fi
    if grep -qE '^AGENT\ta[0-9a-f]+\tgeneral-purpose\t[A-Z_]+\tdeclared$' <<<"$census_out"; then
        pass "27b: the opaque-id dispatch reads as DECLARED (its EXPECT row paired by type)"
    else
        fail "27b: dispatch not declared in the census — $(grep '^AGENT' <<<"$census_out" || echo '(no AGENT lines)')"
    fi
    echo "    ----- 27b census dump -----"
    printf '%s\n' "$census_out" | sed 's/^/      /'
    echo "    ----- end census dump -----"
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
