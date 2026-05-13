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
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"block","permissionDecisionReason":"CA-8 spike: block hook"}}))'
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

# ── Sub-run C: allow-only baseline ───────────────────────────────────────────
# Confirms the harness path actually allows the tool through when no block
# hook is present. Distinguishes "block won" from "tool never ran for other
# reasons" in the two-hook runs above.
scenario "13c ca8_order: allow-only baseline — does the tool run with no block hook?"

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

allow_fired_c=0; grep -q HOOK_ALLOW_FIRED "$HOOK_LOG" && allow_fired_c=1
tool_ran_c=0;    [[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && tool_ran_c=1

claude_exit

# ── Report ───────────────────────────────────────────────────────────────────
echo ""
echo "=========================================================================="
echo "  CA-8 PreToolUse ordering result"
echo "=========================================================================="
echo "  13a (allow, block): allow_fired=$allow_fired_a block_fired=$block_fired_a  tool_ran=$tool_ran_a"
echo "  13b (block, allow): allow_fired=$allow_fired_b block_fired=$block_fired_b  tool_ran=$tool_ran_b"
echo "  13c (allow-only):   allow_fired=$allow_fired_c  tool_ran=$tool_ran_c  [baseline]"
echo ""
echo "Discriminator: number of FIRED lines per sub-run distinguishes"
echo "'both hooks ran' from 'short-circuit on first decision'."
echo "  13a FIRED count: $(grep -c FIRED "$HOOK_LOG" 2>/dev/null || echo 0)"
echo "  13b FIRED count: $(grep -c FIRED "$HOOK_LOG" 2>/dev/null || echo 0)"
echo ""
echo "Interpretation map (decision tree):"
echo "  baseline tool_ran_c=0          -> harness setup is broken; spike inconclusive"
echo "  both hooks fire (count==2 in each sub-run):"
echo "    13a tool_ran != 13b tool_ran -> registration-order-wins (the second decision overrides)"
echo "    13a tool_ran == 13b tool_ran -> AND-semantics: block always wins regardless of order"
echo "  only one hook fires (count==1 in either sub-run):"
echo "                                 -> short-circuit on first decision; registration-order-wins"
echo "                                    (the second hook never gets to run)"
echo ""
echo "Persist outcome to T2:"
echo "  mcp__plugin_nx_nexus__memory_put project=nexus_rdr"
echo "                                   title=111-research-CA-8-spike"
echo "                                   tags=rdr-111,spike,CA-8"
echo "                                   ttl=0"
echo ""
echo "Then update RDR-111 §Step 2 with the confirmed registration strategy."
