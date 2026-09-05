#!/usr/bin/env bash
# Scenario 30 — REAL-WORLD (nexus-ftpk3 follow-on, route 1): an agent whose
# frontmatter declares its OWN Serena MCP server (--project-from-cwd), dispatched
# with isolation:"worktree". Scenario 11 proved inline mcpServers are scoped to
# the subagent and spawn at dispatch; the open question here is whether that
# spawn happens with the WORKTREE as cwd, so Serena roots itself there.
#
# Forensic, not self-report: the agent edits app.py through Serena's
# replace_content. Pass = the edit is in the worktree copy AND the primary's
# app.py is byte-unchanged. No sn plugin is installed; the sn guard keys on the
# mcp__plugin_sn_serena__ prefix and does not apply to mcp__serena-wt__.
#
# Serena is launched for real from the revision sn pins; a cold uv cache under
# TEST_HOME bootstraps it, hence the long waits.

scenario "30 worktree_scoped_serena: agent-scoped Serena roots itself in the dispatch worktree"

SERENA_REV="$(python3 -c "import json;a=json.load(open('$REPO_ROOT/sn/.mcp.json'))['serena']['args'];print(a[a.index('--from')+1])")"

write_agent "worktree-serena-agent" /dev/stdin <<EOF2
---
name: worktree-serena-agent
description: Validation agent — owns a Serena MCP server rooted at its own cwd
mcpServers:
  - serena-wt:
      type: stdio
      command: uvx
      args: ["--from", "$SERENA_REV", "serena", "start-mcp-server", "--context", "claude-code"]
tools: [mcp__serena-wt__activate_project, mcp__serena-wt__get_current_config, mcp__serena-wt__replace_content, mcp__serena-wt__find_symbol, mcp__serena-wt__initial_instructions, Bash, Read]
---

You are validating where a Serena server roots itself. Do EXACTLY this:

1. Run \`pwd\` with Bash and note the directory.
2. Call mcp__serena-wt__activate_project with project=<that exact pwd directory>. Do not skip this: the server starts with NO active project on purpose.
3. Call mcp__serena-wt__replace_content with relative_path="app.py", needle="zzz-marker", repl="yyy-marker", mode="literal".
4. Run \`git status --short\` with Bash.
5. Reply with three lines: CWD=<the pwd output>; SERENA=<one word: DONE if the replace call succeeded, else ERROR followed by the exact error>; then the literal token PROBE-DONE-WT30.
EOF2

cat > "$TEST_HOME/.claude/settings.json" <<'EOF2'
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent", "Bash", "Read", "mcp__serena-wt__*"], "defaultMode": "acceptEdits" }
}
EOF2

WTPROJ="$TEST_HOME/wtproj30"
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

claude_prompt "Use the Agent tool with subagent_type worktree-serena-agent, description 'wt serena root check', and isolation set to worktree. Prompt for the subagent: 'Follow your instructions exactly.' When it returns, repeat its reply verbatim."

poll_for "PROBE-DONE-WT30" 480 "worktree-scoped serena sentinel" || true
OUT=$(capture -600)

WT_DIR=""
for d in "$WTPROJ"/.claude/worktrees/*/; do [[ -d "$d" ]] && WT_DIR="${d%/}" && break; done
if [[ -n "$WT_DIR" ]]; then
    pass "worktree created at $WT_DIR"
else
    fail "no worktree under $WTPROJ/.claude/worktrees — isolation dispatch did not happen"
fi

if echo "$OUT" | grep -q "SERENA=DONE"; then
    pass "Serena replace_content reported success"
else
    fail "Serena replace_content did not report success"
    echo "$OUT" | grep -E "SERENA=|CWD=" | sed 's/^/    | /'
fi

if [[ -n "$WT_DIR" ]] && grep -q "yyy-marker" "$WT_DIR/app.py" 2>/dev/null; then
    pass "edit landed in the WORKTREE copy — Serena rooted itself at the worktree"
else
    fail "worktree app.py does not carry the edit — Serena did not root at the worktree"
fi

if grep -q "zzz-marker" "$WTPROJ/app.py"; then
    pass "primary checkout app.py unchanged"
else
    fail "primary checkout app.py was modified — the agent-scoped server wrote to the primary"
fi

claude_exit
send_keys "cd $REPO_ROOT" Enter
scenario_end
