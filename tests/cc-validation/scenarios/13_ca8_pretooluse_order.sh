#!/usr/bin/env bash
# Scenario 13 — CA-8 spike (nexus-yi08): PreToolUse multi-hook ordering.
#
# Two PreToolUse hooks registered on the same matcher, returning conflicting
# permissionDecision values. Run twice with the registration order swapped to
# discover which decision wins.
#
# Output:
#   - $HOOK_LOG records firing order via timestamped "HOOK_{A,B}_FIRED" lines.
#   - $STUB_LOG records whether the tool actually ran (i.e. an "allow" survived).
#
# The two sub-runs answer the question the RDR-111 §Step 1b spike asks: can
# the ORB hook bridge register first-in-chain and expect its decision to
# win, or must it register last / emit no permissionDecision key at all?
#
# Persist findings to T2: nexus_rdr/111-research-CA-8-spike.

mkdir -p "$TEST_HOME/.claude"

cat > "$TEST_HOME/.claude/hook_allow.sh" <<'EOF'
#!/usr/bin/env bash
INPUT=$(cat)
echo "[$(date +%s%N)] HOOK_ALLOW_FIRED: $INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}))'
EOF
chmod +x "$TEST_HOME/.claude/hook_allow.sh"

cat > "$TEST_HOME/.claude/hook_block.sh" <<'EOF'
#!/usr/bin/env bash
INPUT=$(cat)
echo "[$(date +%s%N)] HOOK_BLOCK_FIRED: $INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"CA-8 spike: block hook"}}))'
EOF
chmod +x "$TEST_HOME/.claude/hook_block.sh"

# ── Sub-run A: allow registered FIRST, block SECOND ──────────────────────────
scenario "13a ca8_order: allow-first then block-second — which decision wins?"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "defaultMode": "default" },
  "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
  },
  "hooks": {
    "PreToolUse": [
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/hook_allow.sh" }] },
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/hook_block.sh" }] }
    ]
  }
}
EOF

: > "$HOOK_LOG"
: > "$STUB_LOG"
claude_start
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

allow_fired_a=0; grep -q HOOK_ALLOW_FIRED "$HOOK_LOG" && allow_fired_a=1
block_fired_a=0; grep -q HOOK_BLOCK_FIRED "$HOOK_LOG" && block_fired_a=1
tool_ran_a=0;    [[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && tool_ran_a=1

claude_exit

# ── Sub-run B: block registered FIRST, allow SECOND ──────────────────────────
scenario "13b ca8_order: block-first then allow-second — which decision wins?"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "defaultMode": "default" },
  "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
  },
  "hooks": {
    "PreToolUse": [
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/hook_block.sh" }] },
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/hook_allow.sh" }] }
    ]
  }
}
EOF

: > "$HOOK_LOG"
: > "$STUB_LOG"
claude_start
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

allow_fired_b=0; grep -q HOOK_ALLOW_FIRED "$HOOK_LOG" && allow_fired_b=1
block_fired_b=0; grep -q HOOK_BLOCK_FIRED "$HOOK_LOG" && block_fired_b=1
tool_ran_b=0;    [[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && tool_ran_b=1

claude_exit

# ── Report ───────────────────────────────────────────────────────────────────
echo ""
echo "=========================================================================="
echo "  CA-8 PreToolUse ordering result"
echo "=========================================================================="
echo "  13a (allow, block): allow_fired=$allow_fired_a block_fired=$block_fired_a  tool_ran=$tool_ran_a"
echo "  13b (block, allow): allow_fired=$allow_fired_b block_fired=$block_fired_b  tool_ran=$tool_ran_b"
echo ""
echo "Interpretation map:"
echo "  Both hooks fire + tool_ran differs by registration order"
echo "                                  -> registration-order wins (first or last)"
echo "  Both hooks fire + tool_ran same"
echo "                                  -> one decision unconditionally wins (block wins?)"
echo "  Only one hook fires             -> Claude Code short-circuits on first decision"
echo ""
echo "Persist outcome to T2:"
echo "  mcp__plugin_nx_nexus__memory_put project=nexus_rdr"
echo "                                   title=111-research-CA-8-spike"
echo "                                   tags=rdr-111,spike,CA-8"
echo "                                   ttl=0"
echo ""
echo "Then update RDR-111 §Step 2 with the confirmed registration strategy."
