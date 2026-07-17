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

# ── 21d: PRODUCTION hook, block round-trip for a zero-SendMessage ────────────
# ── background teammate (RDR-184 P1.4, nexus-ccs9v.10 — gates .15) ───────────
# Runs the REAL conexus/hooks/scripts/subagent-stop.sh (via a logging
# passthrough wrapper), with NX_ORCH_STOP_GUARD=block + XDG_STATE_HOME
# exported in the PANE so the claude child — and therefore the hook —
# inherits them the same way a production session's env reaches hooks.
# The orchestrator-side EXPECT row is planted BEFORE the dispatch prompt
# (write-before-dispatch), keyed to the spawner session_id captured by a
# SessionStart hook (21b: teammates fire SubagentStop in the SPAWNER's
# session).
scenario "21d production hook: zero-SendMessage background teammate is blocked once, then complies"

ORCH_STATE="$TEST_HOME/.local/state"
ORCH_LOG="$TEST_HOME/orch21d.log"
SID_FILE="$TEST_HOME/orch21d.sid"
rm -f "$ORCH_LOG" "$SID_FILE"
rm -rf "$ORCH_STATE/nexus/orchestration"

cat > "$TEST_HOME/.claude/orch_sid.sh" <<SID_EOF
#!/usr/bin/env bash
python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' >> "$SID_FILE"
exit 0
SID_EOF
chmod +x "$TEST_HOME/.claude/orch_sid.sh"

# Logging passthrough around the REAL hook: records every payload and any
# decision the hook emits, while delivering the decision to Claude Code
# unchanged. Env (guard mode, state home) is inherited from claude, NOT
# injected here — that inheritance path is part of what this leg validates.
cat > "$TEST_HOME/.claude/orch_stop_wrap.sh" <<WRAP_EOF
#!/usr/bin/env bash
payload="\$(cat)"
printf 'STOP_PAYLOAD %s\n' "\$payload" >> "$ORCH_LOG"
out="\$(printf '%s' "\$payload" | bash "$REPO_ROOT/conexus/hooks/scripts/subagent-stop.sh" 2>>"$ORCH_LOG")"
if [[ -n "\$out" ]]; then
    printf 'STOP_DECISION %s\n' "\$out" >> "$ORCH_LOG"
    printf '%s\n' "\$out"
fi
exit 0
WRAP_EOF
chmod +x "$TEST_HOME/.claude/orch_stop_wrap.sh"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent", "SendMessage"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SessionStart": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/orch_sid.sh", "timeout": 10 }] }
    ],
    "SubagentStop": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/orch_stop_wrap.sh", "timeout": 15 }] }
    ]
  }
}
EOF

# Arm the guard in the PANE env before launching claude (unset at leg end —
# reset_scenario_state does not clean pane env).
send_keys "export NX_ORCH_STOP_GUARD=block XDG_STATE_HOME=$ORCH_STATE" Enter
sleep 1

claude_start

SID=""
for _ in $(seq 1 60); do
    SID="$(head -1 "$SID_FILE" 2>/dev/null)"
    [[ -n "$SID" ]] && break
    sleep 1
done

if [[ -z "$SID" ]]; then
    fail "21d setup: SessionStart hook never captured a session_id"
else
    pass "21d setup: spawner session_id captured ($SID)"
    # Orchestrator write path: EXPECT row BEFORE the dispatch prompt.
    (
        export XDG_STATE_HOME="$ORCH_STATE"
        # shellcheck source=../../e2e/lib/expectations.sh disable=SC1091
        source "$REPO_ROOT/tests/e2e/lib/expectations.sh"
        expectations_expect "$SID" "probe21d" "background"
    )
    EXPFILE="$ORCH_STATE/nexus/orchestration/$SID.expectations"
    if [[ -f "$EXPFILE" ]] && grep -q "EXPECT" "$EXPFILE"; then
        pass "21d setup: EXPECT row planted before dispatch ($EXPFILE)"
    else
        fail "21d setup: EXPECT row missing from $EXPFILE"
    fi

    claude_prompt "Use the Agent tool to spawn a background agent: subagent_type='general-purpose', name='probe21d', run_in_background=true, prompt='Reply with exactly BG-21D-DONE and finish. Do NOT use SendMessage or any other tool. Exception: if you are later explicitly instructed to send a completion report, send it via SendMessage including the exact token COMPLY-21D, then stop.' Immediately after spawning (do NOT wait for it), reply SPAWNED-21D and stop."
    claude_wait 90
    # Teammate lifecycle: run -> stop -> BLOCK -> continue -> SendMessage -> re-stop.
    sleep 75
    capture -100 >/dev/null 2>&1 || true

    if grep "^STOP_DECISION " "$ORCH_LOG" 2>/dev/null | grep -q '"decision": "block"'; then
        pass "21d: production hook emitted a block decision for the silent teammate"
    else
        fail "21d: no block decision in $ORCH_LOG — $(grep -c '^STOP_PAYLOAD ' "$ORCH_LOG" 2>/dev/null || echo 0) payloads seen"
    fi
    if awk -F'\t' '$2=="BLOCKED" && $3 ~ /^aprobe21d-/ {found=1} END{exit !found}' "$EXPFILE" 2>/dev/null; then
        pass "21d: durable BLOCKED once-guard row recorded for aprobe21d-*"
    else
        fail "21d: no BLOCKED row for aprobe21d-* in $EXPFILE"
    fi
    if grep "^STOP_PAYLOAD " "$ORCH_LOG" 2>/dev/null | grep "aprobe21d-" | grep -q '"stop_hook_active":true'; then
        pass "21d: round-trip completed — teammate re-stopped with stop_hook_active=true (blocked exactly once)"
    else
        fail "21d: no re-stop with stop_hook_active=true for aprobe21d-* — block round-trip did not complete"
    fi
    # Compliance evidence: the teammate's own transcript contains a
    # SendMessage tool_use (scan the transcript path from its stop payload).
    COMPLY="$(python3 - "$ORCH_LOG" <<'PY_EOF'
import json, sys
path = None
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if not line.startswith("STOP_PAYLOAD "):
        continue
    try:
        p = json.loads(line[len("STOP_PAYLOAD "):])
    except json.JSONDecodeError:
        continue
    if str(p.get("agent_id", "")).startswith("aprobe21d-"):
        path = p.get("agent_transcript_path") or path
found = False
if path:
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            if '"SendMessage"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "assistant":
                continue
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "SendMessage":
                    found = True
    except OSError:
        pass
print("YES" if found else "NO")
PY_EOF
)"
    if [[ "$COMPLY" == "YES" ]]; then
        pass "21d: teammate complied — SendMessage tool_use present in its transcript after the block"
    else
        fail "21d: no SendMessage tool_use in the teammate transcript — compliance not observed"
    fi
fi

claude_exit
send_keys "unset NX_ORCH_STOP_GUARD XDG_STATE_HOME" Enter
sleep 1
scenario_end

# ── 21e: PRODUCTION hook, sync dispatch that used SendMessage mid-run ────────
# ── is NOT blocked — adversarial: an EXPECT background row is planted ────────
# ── for a name equal to the sync dispatch's subagent_type ────────────────────
# The named-morphology consult rule must refuse the collision: a sync
# unnamed dispatch has agent_id "a<hash>" (no "a<name>-" prefix), so even a
# matching-name EXPECT row can never block it (gate-critique Significant-3).
scenario "21e production hook: sync dispatch with mid-run SendMessage is never blocked"

ORCH_LOG_E="$TEST_HOME/orch21e.log"
SID_FILE_E="$TEST_HOME/orch21e.sid"
rm -f "$ORCH_LOG_E" "$SID_FILE_E"

cat > "$TEST_HOME/.claude/orch_sid.sh" <<SID_EOF
#!/usr/bin/env bash
python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' >> "$SID_FILE_E"
exit 0
SID_EOF
chmod +x "$TEST_HOME/.claude/orch_sid.sh"

cat > "$TEST_HOME/.claude/orch_stop_wrap.sh" <<WRAP_EOF
#!/usr/bin/env bash
payload="\$(cat)"
printf 'STOP_PAYLOAD %s\n' "\$payload" >> "$ORCH_LOG_E"
out="\$(printf '%s' "\$payload" | bash "$REPO_ROOT/conexus/hooks/scripts/subagent-stop.sh" 2>>"$ORCH_LOG_E")"
if [[ -n "\$out" ]]; then
    printf 'STOP_DECISION %s\n' "\$out" >> "$ORCH_LOG_E"
    printf '%s\n' "\$out"
fi
exit 0
WRAP_EOF
chmod +x "$TEST_HOME/.claude/orch_stop_wrap.sh"
# settings.json identical shape to 21d — rewrite (reset_scenario_state does
# not run between legs).
cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent", "SendMessage"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SessionStart": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/orch_sid.sh", "timeout": 10 }] }
    ],
    "SubagentStop": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/orch_stop_wrap.sh", "timeout": 15 }] }
    ]
  }
}
EOF

send_keys "export NX_ORCH_STOP_GUARD=block XDG_STATE_HOME=$ORCH_STATE" Enter
sleep 1

claude_start

SID_E=""
for _ in $(seq 1 60); do
    SID_E="$(head -1 "$SID_FILE_E" 2>/dev/null)"
    [[ -n "$SID_E" ]] && break
    sleep 1
done

if [[ -z "$SID_E" ]]; then
    fail "21e setup: SessionStart hook never captured a session_id"
else
    pass "21e setup: spawner session_id captured ($SID_E)"
    (
        export XDG_STATE_HOME="$ORCH_STATE"
        # shellcheck source=../../e2e/lib/expectations.sh disable=SC1091
        source "$REPO_ROOT/tests/e2e/lib/expectations.sh"
        expectations_expect "$SID_E" "general-purpose" "background"
    )
    EXPFILE_E="$ORCH_STATE/nexus/orchestration/$SID_E.expectations"

    claude_prompt "Use Task to dispatch the general-purpose agent (a normal SYNC dispatch, no name, not in background). Description='21e probe'. Prompt: 'You MUST use the SendMessage tool. If it is not in your loaded tools, first run ToolSearch with query select:SendMessage to load its schema. Then call SendMessage with to=main and content=SYNC-21E-PING. Only after the SendMessage call, reply with exactly SYNC-21E-DONE and finish.' After it returns, reply DISPATCH-21E-COMPLETE and stop."
    claude_wait 150
    sleep 10
    capture -100 >/dev/null 2>&1 || true

    if grep "^STOP_PAYLOAD " "$ORCH_LOG_E" 2>/dev/null | grep -q "SYNC-21E-DONE"; then
        pass "21e: sync dispatch's SubagentStop reached the production hook"
    else
        fail "21e setup: no stop payload with the sync marker — hook never consulted"
    fi
    # Setup non-vacuity: the sync agent genuinely used SendMessage mid-run
    # (else this leg proves nothing about the false-block class).
    # Scan the SYNC agent's transcript (payload selected by its final-message
    # marker, not just "last payload") for a SendMessage tool_use. Verdict on
    # line 1; diagnostic detail after it lands in the runner stdout — the
    # transcript itself dies with TEST_HOME at runner exit, so the diagnosis
    # must be captured HERE, not post-hoc.
    MIDRUN_OUT="$(python3 - "$ORCH_LOG_E" <<'PY_EOF'
import json, sys
paths = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if not line.startswith("STOP_PAYLOAD "):
        continue
    try:
        p = json.loads(line[len("STOP_PAYLOAD "):])
    except json.JSONDecodeError:
        continue
    tp = p.get("agent_transcript_path") or ""
    if tp:
        paths.append((tp, "SYNC-21E-DONE" in line))
marked = [tp for tp, m in paths if m] or [tp for tp, _ in paths]
found = False
tools_seen: list[str] = []
detail = f"payloads_with_path={len(paths)} scanned={marked}"
for path in marked:
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "assistant":
                continue
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools_seen.append(str(b.get("name")))
                    if b.get("name") == "SendMessage":
                        found = True
    except OSError as exc:
        detail += f" open_error={exc!r}"
print("YES" if found else "NO")
print(f"    21e scan: {detail} tool_uses={tools_seen}")
PY_EOF
)"
    MIDRUN="$(head -1 <<<"$MIDRUN_OUT")"
    tail -n +2 <<<"$MIDRUN_OUT"
    if [[ "$MIDRUN" == "YES" ]]; then
        pass "21e setup non-vacuity: sync agent genuinely used SendMessage mid-run"
    else
        fail "21e setup non-vacuity: no SendMessage tool_use in the sync agent transcript — leg proves nothing"
    fi
    if grep -q "^STOP_DECISION " "$ORCH_LOG_E" 2>/dev/null; then
        fail "21e FALSE BLOCK: production hook emitted a decision for a sync dispatch — $(grep '^STOP_DECISION ' "$ORCH_LOG_E" | head -c 300)"
    else
        pass "21e: no block decision for the sync dispatch (morphology refused the adversarial name collision)"
    fi
    if [[ ! -f "$EXPFILE_E" ]] || ! grep -q "BLOCKED" "$EXPFILE_E"; then
        pass "21e: no BLOCKED row recorded"
    else
        fail "21e: BLOCKED row present in $EXPFILE_E — sync dispatch was marked"
    fi
fi

claude_exit
send_keys "unset NX_ORCH_STOP_GUARD XDG_STATE_HOME" Enter
sleep 1
rm -rf "$ORCH_STATE/nexus/orchestration"
rm -f "$ORCH_LOG" "$ORCH_LOG_E" "$SID_FILE" "$SID_FILE_E" \
      "$TEST_HOME/.claude/orch_sid.sh" "$TEST_HOME/.claude/orch_stop_wrap.sh"
scenario_end
