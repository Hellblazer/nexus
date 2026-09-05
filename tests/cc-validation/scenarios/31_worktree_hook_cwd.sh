#!/usr/bin/env bash
# Scenario 31 — DETERMINATION probe (nexus-ftpk3): for a subagent dispatched
# with isolation:"worktree", what does the `cwd` field carry in the
# SubagentStart payload and in the PreToolUse payloads of the subagent's own
# tool calls? The sn worktree guard keys ENTIRELY on that field. Scenario 30
# showed the agent-scoped MCP server is spawned in the PARENT's cwd, and the
# first honest run of 29 reported WT-SECTION-NONE, so the hook cwd is in doubt.
#
# No plugins, no Serena: two logging hooks dump raw stdin, the subagent runs
# `pwd` (PreToolUse fires) and writes one file into its cwd. Results are read
# from the log, not from model self-report.

scenario "31 worktree_hook_cwd: which cwd do SubagentStart/PreToolUse carry for a worktree subagent?"

HLOG="$TEST_HOME/hook_cwd_events.log"
: > "$HLOG"
cat > "$TEST_HOME/.claude/log_hook_event.sh" <<BASH_EOF
#!/usr/bin/env bash
printf '%s %s\n' "\$1" "\$(cat)" >> "$HLOG"
exit 0
BASH_EOF
chmod +x "$TEST_HOME/.claude/log_hook_event.sh"

cat > "$TEST_HOME/.claude/settings.json" <<SETTINGS_EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent", "Bash", "Write"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "", "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/log_hook_event.sh SUBAGENT_START", "timeout": 10 }] }
    ],
    "PreToolUse": [
      { "matcher": "Bash|Write", "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/log_hook_event.sh PRE_TOOL_USE", "timeout": 10 }] }
    ]
  }
}
SETTINGS_EOF

WTPROJ="$TEST_HOME/wtproj31"
rm -rf "$WTPROJ"; mkdir -p "$WTPROJ"
( cd "$WTPROJ" || exit 1
  git init -q -b main
  echo x > f.txt
  git -c user.name=t -c user.email=t@t add f.txt
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

claude_prompt "Use the Agent tool with subagent_type general-purpose, description 'cwd probe', and isolation set to worktree. Prompt for the subagent: 'Step 1: run the Bash command pwd. Step 2: use the Write tool to create a file named probe31.txt in your current directory containing the pwd output. Step 3: reply with one line: PWD=<the pwd output>, then on its own line the word PROBE-DONE followed by -WT31 joined with no space.'"

poll_for "PROBE-DONE-WT31" 240 "cwd probe sentinel" || true
sleep 3

echo "    --- raw hook events (tag, event, tool, cwd, agent_type) ---"
python3 - "$HLOG" <<'PY' | sed 's/^/    | /'
import json, sys, pathlib
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    tag, _, body = line.partition(" ")
    try: d = json.loads(body)
    except Exception: continue
    print((tag, d.get("hook_event_name"), d.get("tool_name"), d.get("cwd"), d.get("agent_type")))
PY

SUMMARY=$(python3 - "$HLOG" <<'PY'
import json, sys, pathlib
sub=pre=subwt=prewt=0
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    tag,_,body=line.partition(" ")
    try: d=json.loads(body)
    except Exception: continue
    wt = "/.claude/worktrees/" in (d.get("cwd") or "")
    if tag=="SUBAGENT_START": sub+=1; subwt+=wt
    if tag=="PRE_TOOL_USE": pre+=1; prewt+=wt
print(sub,subwt,pre,prewt)
PY
)
read -r SUB SUBWT PRE PREWT <<< "$SUMMARY"

if [[ "$SUB" -ge 1 ]]; then pass "SubagentStart fired ($SUB)"; else fail "SubagentStart never fired"; fi
if [[ "$SUBWT" -ge 1 ]]; then
    pass "SubagentStart cwd IS the worktree ($SUBWT/$SUB)"
else
    fail "SubagentStart cwd is NOT the worktree ($SUBWT/$SUB) — the sn guard cannot detect worktree dispatch at injection time"
fi
if [[ "$PRE" -ge 1 && "$PREWT" -ge 1 ]]; then
    pass "PreToolUse cwd IS the worktree for subagent tool calls ($PREWT/$PRE)"
else
    fail "PreToolUse cwd is NOT the worktree for subagent tool calls ($PREWT/$PRE) — the deny guard cannot key on cwd"
fi

WT_DIR=""
for d in "$WTPROJ"/.claude/worktrees/*/; do [[ -d "$d" ]] && WT_DIR="${d%/}" && break; done
if [[ -n "$WT_DIR" && -f "$WT_DIR/probe31.txt" ]]; then
    pass "subagent's Write landed in the worktree ($WT_DIR/probe31.txt)"
elif [[ -f "$WTPROJ/probe31.txt" ]]; then
    fail "subagent's Write landed in the PRIMARY"
else
    fail "probe31.txt not found in worktree or primary (worktree may have been auto-cleaned; see raw events above)"
fi

claude_exit
send_keys "cd $REPO_ROOT" Enter
scenario_end
