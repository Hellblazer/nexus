#!/usr/bin/env bash
# Scenario 29 — REAL-WORLD (nexus-ftpk3): the sn worktree guard, end to end,
# with FORENSIC evidence rather than model self-report.
#
# Topology: claude runs in a throwaway git project; a general-purpose agent is
# dispatched with isolation:"worktree". Scenario 31 established that both
# SubagentStart and PreToolUse carry the WORKTREE cwd for such an agent; this
# scenario proves the two sn hook scripts act on it:
#
#   1. SubagentStart: sn/hooks/scripts/mcp-inject.sh (wired directly from
#      settings.json through a tee wrapper, so its raw envelope is logged)
#      emits the "Worktree isolation" section.
#   2. PreToolUse: sn/hooks/scripts/auto-approve-sn-mcp.sh DENIES
#      mcp__plugin_sn_serena__replace_in_files. The Serena stand-in is
#      fixtures/stub_serena_server.py registered under the server name
#      plugin_sn_serena (so the tool names match the real prefix) — proof of
#      the deny is the ABSENCE of a replace_in_files line in STUB_LOG, while a
#      READ tool (find_symbol) from the same worktree DOES reach the stub.
#
# History: v1 matched probe tokens that were verbatim in the typed prompt
# (vacuous); v2 asked the model whether the section was present and could not
# reach the real plugin's Serena in this harness. v3 reads logs.

scenario "29 sn_worktree_guard: worktree subagent gets the section; Serena writes denied, reads allowed"

INJECT_LOG="$TEST_HOME/sn_inject_envelope.log"
: > "$INJECT_LOG"; : > "$STUB_LOG"
cat > "$TEST_HOME/.claude/sn_inject_tee.sh" <<BASH_EOF
#!/usr/bin/env bash
out="\$(bash "$REPO_ROOT/sn/hooks/scripts/mcp-inject.sh")"
printf '%s\n' "\$out" >> "$INJECT_LOG"
printf '%s\n' "\$out"
BASH_EOF
chmod +x "$TEST_HOME/.claude/sn_inject_tee.sh"

cat > "$TEST_HOME/.claude/settings.json" <<SETTINGS_EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent", "Bash", "mcp__plugin_sn_serena__*"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "", "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/sn_inject_tee.sh", "timeout": 10 }] }
    ],
    "PreToolUse": [
      { "matcher": "mcp__plugin_sn_serena__.*", "hooks": [{ "type": "command", "command": "bash $REPO_ROOT/sn/hooks/scripts/auto-approve-sn-mcp.sh", "timeout": 10 }] }
    ]
  }
}
SETTINGS_EOF
cat > "$TEST_HOME/.mcp.json" <<MCP_EOF
{ "mcpServers": {
    "plugin_sn_serena": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_serena_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
} }
MCP_EOF

WTPROJ="$TEST_HOME/wtproj29"
rm -rf "$WTPROJ"; mkdir -p "$WTPROJ"
( cd "$WTPROJ" || exit 1
  git init -q -b main
  printf 'def hello():\n    return "zzz-marker"\n' > app.py
  git -c user.name=t -c user.email=t@t add app.py
  git -c user.name=t -c user.email=t@t commit -q -m init )
python3 - "$TEST_HOME/.claude.json" "$WTPROJ" <<'PY'
import json, os, pathlib, sys
cfg = pathlib.Path(sys.argv[1]); p = sys.argv[2]
try: data = json.loads(cfg.read_text())
except Exception: data = {}
for key in {p, os.path.realpath(p)}:
    e = data.setdefault("projects", {}).setdefault(key, {})
    e["hasTrustDialogAccepted"] = True; e["hasCompletedProjectOnboarding"] = True
cfg.write_text(json.dumps(data, indent=2))
PY
send_keys "cd $WTPROJ" Enter
sleep 1
claude_start

claude_prompt "Use the Agent tool with subagent_type general-purpose, description 'wt guard check', and isolation set to worktree. Prompt for the subagent: 'This is a test of a permission hook, so you MUST actually attempt every tool call below even if guidance in your context tells you not to; the hook is what is being measured, not your judgement. Do exactly these steps, in order. Step 1: call mcp__plugin_sn_serena__find_symbol with name_path_pattern=hello. Step 2: call mcp__plugin_sn_serena__replace_in_files with needle=zzz-marker repl=yyy-marker mode=literal. Step 3: reply with one line per step saying whether the call ran or was refused by the system, quoting the refusal text verbatim, then on its own line the word PROBE-DONE followed by -WT29 joined with no space.'"

poll_for "PROBE-DONE-WT29" 300 "worktree guard sentinel" || true
sleep 3
OUT=$(capture -600)

# 1. Injection: the logged envelope (not the model) says whether the section went out.
if python3 - "$INJECT_LOG" <<'PY'
import json, sys, pathlib
ok = False
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    try: body = json.loads(line)["hookSpecificOutput"]["additionalContext"]
    except Exception: continue
    if "## Worktree isolation" in body and body.index("## Worktree isolation") < body.index("## Serena MCP"):
        ok = True
sys.exit(0 if ok else 1)
PY
then pass "mcp-inject.sh emitted the worktree section first for the worktree subagent (envelope logged)"
else fail "no logged SubagentStart envelope carries the worktree section"; sed 's/^/    | /' "$INJECT_LOG" | cut -c1-200 | head -5
fi

# 2. Reads allowed: find_symbol reached the stub from the worktree.
if grep -q '"tool": "find_symbol"' "$STUB_LOG"; then
    pass "Serena READ tool (find_symbol) reached the server from the worktree"
else
    fail "find_symbol never reached the stub — reads are blocked or the stub never connected"
    grep -c . "$STUB_LOG" | sed 's/^/    | stub log lines: /'
fi

# 3. Writes denied: replace_in_files must NOT reach the stub, and the deny reason must be visible.
if grep -q '"tool": "replace_in_files"' "$STUB_LOG"; then
    fail "replace_in_files REACHED the server from a worktree — the guard did not deny"
else
    if grep -q '"tool": "find_symbol"' "$STUB_LOG"; then
        pass "replace_in_files never reached the server (denied before dispatch)"
    else
        fail "replace_in_files absent from stub log, but so is find_symbol — inconclusive (server not connected?)"
    fi
fi
if echo "$OUT" | grep -q "sn worktree guard"; then
    pass "deny reason 'sn worktree guard' surfaced to the subagent"
else
    fail "deny reason not seen in the pane"
    echo "$OUT" | grep -iE "refus|denied|replace_in_files" | head -5 | sed 's/^/    | /'
fi
if grep -q "zzz-marker" "$WTPROJ/app.py"; then
    pass "primary checkout app.py unchanged"
else
    fail "primary checkout app.py was modified"
fi

claude_exit
send_keys "cd $REPO_ROOT" Enter
scenario_end
