#!/usr/bin/env bash
# Scenario 29 — REAL-WORLD (nexus-ftpk3): install sn from this repo into
# TEST_HOME, launch claude in a throwaway git project, dispatch a subagent
# with isolation:"worktree", and check two things that only the in-tree
# hooks produce:
#
#   1. mcp-inject.sh saw a linked-worktree cwd and injected
#      worktree-section.md ("Worktree isolation" header) — WT-SECTION-OK.
#   2. auto_approve_sn_mcp.py DENIED a Serena write tool from that cwd with
#      a reason containing "sn worktree guard" — DENY-OK.
#
# Background: Serena resolves paths against the root fixed at server start,
# so a worktree subagent's Serena edits landed in the primary checkout
# (three incidents). Unit tests drive both scripts with synthetic payloads;
# this scenario is the end-to-end question: does Claude Code hand the
# WORKTREE cwd (not the parent's) to SubagentStart and PreToolUse, and does
# the deny actually stop the call.
#
# Serena is launched for real (uvx from the pinned revision); a cold uv
# cache under TEST_HOME can take a minute or two, hence the long poll.

scenario "29 sn_worktree_guard: worktree subagent gets the section and Serena writes are denied"

NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<EOF2
{
  "version": 2,
  "plugins": {
    "sn@nexus-plugins": [
      { "scope": "user", "installPath": "$REPO_ROOT/sn", "version": "dev",
        "installedAt": "$NOW", "lastUpdated": "$NOW" }
    ]
  }
}
EOF2
cat > "$TEST_HOME/.claude/settings.json" <<'EOF2'
{
  "skipDangerousModePermissionPrompt": true,
  "enabledPlugins": { "sn@nexus-plugins": true },
  "permissions": { "allow": ["Task", "Agent"], "defaultMode": "acceptEdits" }
}
EOF2

# A real git project for claude to run in; the worktree dispatch creates a
# linked worktree under it.
WTPROJ="$TEST_HOME/wtproj"
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
# Give the Serena MCP server time to come up before the subagent needs it.
sleep 45

claude_prompt "Use the Agent tool with subagent_type general-purpose, description 'wt guard check', and isolation set to worktree. Prompt for the subagent: 'Do exactly these steps. Step 1: on its own line output WT-SECTION-OK if the guidance injected into your context contains the header text Worktree isolation, otherwise output WT-SECTION-NONE. Step 2: call the tool mcp__plugin_sn_serena__replace_in_files with needle=zzz-marker repl=yyy-marker mode=literal (load its schema with ToolSearch first if needed). Step 3: on its own line output DENY-OK if that call was refused and the refusal text contains sn worktree guard; output ALLOW-BAD if the call ran and reported any replacements or success; otherwise output ERR-OTHER followed by the exact error text. Step 4: on its own line output the literal token PROBE-DONE-WT21.'"

poll_for "PROBE-DONE-WT21" 420 "worktree subagent reply sentinel" || true
OUT=$(capture -600)

SECTION=0; DENY=0; ALLOW_BAD=0; OTHER=0
echo "$OUT" | grep -q "WT-SECTION-OK" && SECTION=1
echo "$OUT" | grep -q "DENY-OK" && DENY=1
echo "$OUT" | grep -q "ALLOW-BAD" && ALLOW_BAD=1
echo "$OUT" | grep -q "ERR-OTHER" && OTHER=1

if [[ $SECTION -eq 1 ]]; then
    pass "worktree section reached the worktree subagent (SubagentStart saw the worktree cwd)"
else
    fail "worktree section NOT in subagent context (WT-SECTION-NONE or indeterminate)"
fi
if [[ $DENY -eq 1 ]]; then
    pass "Serena replace_in_files denied by the sn worktree guard from the worktree cwd"
elif [[ $ALLOW_BAD -eq 1 ]]; then
    fail "Serena replace_in_files was ALLOWED from a worktree — the guard did not fire"
elif [[ $OTHER -eq 1 ]]; then
    fail "Serena call errored for another reason (MCP not connected?) — deny path not exercised"
    echo "$OUT" | grep -A3 "ERR-OTHER" | sed 's/^/    | /'
else
    fail "indeterminate — no DENY-OK/ALLOW-BAD/ERR-OTHER token seen"
    echo "$OUT" | tail -40 | sed 's/^/    | /'
fi

# The primary must be untouched whatever the subagent did.
if grep -q "zzz-marker" "$WTPROJ/app.py"; then
    pass "primary checkout app.py unchanged"
else
    fail "primary checkout app.py was modified — a write escaped the worktree"
fi

cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<'EOF2'
{"version": 2, "plugins": {}}
EOF2
claude_exit
send_keys "cd $REPO_ROOT" Enter
scenario_end
