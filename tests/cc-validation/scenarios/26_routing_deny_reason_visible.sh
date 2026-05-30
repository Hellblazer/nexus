#!/usr/bin/env bash
# Scenario 26 — does a PreToolUse:Bash routing deny actually surface its
# reason to the model? (RDR-121 grep->Serena redirect, deny-reason-uix fix.)
#
# Regression target: deny_envelope previously emitted only
# hookSpecificOutput.reason, which current Claude Code does not read. A
# deny then arrived as a bare "denied" with no cause/remediation. The fix
# adds permissionDecisionReason + top-level systemMessage. This scenario
# wires the REAL sn grep_for_symbols hook and confirms the model sees the
# redirect text (Serena / ToolSearch / escape-token), not just a block.
#
# Also validates the backend-agnostic message: it must NOT hand the model
# a hardcoded jet_brains_ call line; it must mention both backends / the
# ToolSearch both-variant load.

GREP_HOOK="$REPO_ROOT/sn/hooks/scripts/routing/grep_for_symbols_redirects_to_serena.py"
ROUTING_DIR="$REPO_ROOT/sn/hooks/scripts/routing"

scenario "26 routing deny reason: grep on a code file surfaces the Serena redirect to the model"

# A code file with an identifier to grep for.
mkdir -p "$TEST_HOME/work"
cat > "$TEST_HOME/work/sample.py" <<'PY'
class RenameLockManager:
    pass
PY

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Bash(grep:*)"], "defaultMode": "default" },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [{ "type": "command",
                    "command": "python3 $GREP_HOOK" }] }
    ]
  }
}
EOF

claude_start
# Tell the model to run exactly this grep and then report what it saw.
claude_prompt "Run this exact bash command: grep RenameLockManager $TEST_HOME/work/sample.py — if it is blocked, reply with the literal block message you received, then reply DONE."
claude_wait 90

OUT="$(capture -200)"
claude_exit

# 1. The deny reason text reached the model (not a bare "denied").
saw_serena=0
echo "$OUT" | grep -qi "Serena" && saw_serena=1
saw_remedy=0
echo "$OUT" | grep -qiE "find_symbol|ToolSearch|routing-allow" && saw_remedy=1

# 2. Backend-agnostic: model was NOT handed a hardcoded jet_brains_ call.
#    (A bare mention of the prefix in the "varies by backend" note is fine;
#     a jet_brains_find_symbol( CALL line is the regression.)
hardcoded_call=0
echo "$OUT" | grep -q "jet_brains_find_symbol(name_path" && hardcoded_call=1

echo "    saw_serena=$saw_serena saw_remedy=$saw_remedy hardcoded_call=$hardcoded_call"

if [[ $saw_serena -eq 1 && $saw_remedy -eq 1 && $hardcoded_call -eq 0 ]]; then
    pass "deny reason + backend-agnostic remedies surfaced to the model"
elif [[ $saw_serena -eq 0 && $saw_remedy -eq 0 ]]; then
    fail "model saw a bare deny with no reason — permissionDecisionReason not delivered (the regression)"
elif [[ $hardcoded_call -eq 1 ]]; then
    fail "message handed the model a hardcoded jet_brains_ call line (not backend-agnostic)"
else
    fail "partial: reason text incomplete (saw_serena=$saw_serena saw_remedy=$saw_remedy)"
fi

scenario_end
