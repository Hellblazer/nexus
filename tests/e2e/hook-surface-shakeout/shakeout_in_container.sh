#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-215 hook-surface shakeout, container half. Drives ONE real Claude Code
# session through turns chosen to trigger every populated hook event of the
# plugin under test, captures the pane, and censuses what actually fired
# against what hooks.json declares.
#
# The tmux/launch/prompt helpers below are taken from
# tests/e2e/rdr208-mvv/mvv_in_container.sh rather than re-derived: the private
# socket, the dialog walk, the `exec` so the pane process IS claude, the
# explicit PATH (tmux opens a login shell and drops the image's ENV PATH), and
# the Stop-hook turn sentinel instead of pane scraping. Each of those cost a
# billed run to learn once already.
set -uo pipefail
HOME_DIR=/home/nexus
PLUGIN=$HOME_DIR/plugin
WORK=$HOME_DIR/repo
RUN=$HOME_DIR/run
SOCK=shakeout-sock
PASS=0; FAIL=0
mkdir -p "$RUN"
export MVV_RUN="$RUN"

ok()  { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
say() { printf '\n== %s\n' "$*"; }
now() { date +%s; }
T() { tmux -L "$SOCK" "$@"; }
pane() { T capture-pane -p -t "$1" 2>/dev/null; }
wait_for() { local d=$(( $(now) + $1 )); shift; while [ "$(now)" -lt "$d" ]; do "$@" && return 0; sleep 1; done; return 1; }

# --- credentials: mounted read-only by run.sh, never derived here ----------
say "auth"
mkdir -p "$HOME_DIR/.claude"
umask 077
cp /creds/.credentials.json "$HOME_DIR/.claude/.credentials.json"
umask 022
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/home/nexus/.claude.json")
d = json.loads(p.read_text()) if p.exists() else {}
d["hasCompletedOnboarding"] = True
d.setdefault("projects", {})["/home/nexus/repo"] = {"hasTrustDialogAccepted": True}
p.write_text(json.dumps(d))
PY
claude --version || { echo "no claude CLI"; exit 1; }

say "provision: nx init (local mode, virgin HOME)"
nx init --service --yes > "$RUN/init.log" 2>&1 \
    || { echo "nx init failed"; tail -30 "$RUN/init.log"; exit 1; }
ok "nx init completed"

SID_OF=""
turn_stamp() { local f="$RUN/turn-end.$SID_OF"; [ -f "$f" ] && stat -c %Y "$f" 2>/dev/null || echo 0; }
turn_ended_since() { [ "$(turn_stamp)" -gt "$1" ]; }

prompt() {  # TEXT LABEL: paste, Enter, wait for the Stop-hook turn sentinel
    local before; before="$(turn_stamp)"
    printf '%s' "$1" | T load-buffer -
    T paste-buffer -t S
    sleep 0.5
    T send-keys -t S Enter
    if ! wait_for 240 turn_ended_since "$before"; then
        bad "$2: no turn-end within 240 s"
        pane S | tail -12
        return 1
    fi
    ok "$2"
}

say "launch: real Claude Code, plugin from ${SHAKEOUT_SHA:-?}, ALL hooks live"
mcp="$RUN/mcp.json"
printf '{"mcpServers":{"nexus":{"type":"stdio","command":"%s/nxenv/bin/nx-mcp","args":[],"env":{"NX_MCP_LOG":"%s/nx-mcp.log"}}}}\n' \
    "$HOME_DIR" "$RUN" > "$mcp"
CMD="export PATH=$HOME_DIR/nxenv/bin:$HOME_DIR/.local/bin:\$PATH && cd $WORK && exec claude --debug --dangerously-skip-permissions --plugin-dir $PLUGIN --mcp-config $mcp --strict-mcp-config"
T kill-server 2>/dev/null
T new-session -d -s S -x 240 -y 50 || { echo "tmux failed"; exit 1; }
T pipe-pane -t S -o "cat >> $RUN/pane.log"
T send-keys -t S "$CMD" Enter
deadline=$(( $(now) + 150 ))
while [ "$(now)" -lt "$deadline" ]; do
    p="$(pane S)"
    if grep -qF "bypass permissions on" <<<"$p"; then break
    elif grep -qF "I am using this for local development" <<<"$p"; then T send-keys -t S Enter; sleep 3
    elif grep -qiE "trust this folder|project you trust" <<<"$p"; then T send-keys -t S Enter; sleep 2
    elif grep -qF "Yes, I accept" <<<"$p"; then T send-keys -t S Down; sleep 0.5; T send-keys -t S Enter; sleep 5
    elif grep -qiE "oauth/authorize|Paste code|Login expired|Not logged in" <<<"$p"; then
        bad "the session is not logged in (husk credential?)"; pane S | tail -12; exit 1
    fi
    sleep 1
done
if grep -qF "bypass permissions on" <<<"$(pane S)"; then
    T send-keys -t S Down; sleep 0.5; T send-keys -t S Enter; sleep 6
fi
ok "session launched"

# The session id, from the status record the SessionStart hook writes.
for _ in $(seq 1 30); do
    SID_OF="$(ls -1 "$HOME_DIR/.config/nexus/status" 2>/dev/null | grep -v '\.tmp$' | head -1)"
    [ -n "$SID_OF" ] && break
    sleep 1
done
if [ -n "$SID_OF" ]; then ok "SessionStart produced a status record ($SID_OF)"
else bad "no status record: the SessionStart battery did not run"; fi

# --- the turns, each aimed at one event -----------------------------------
say "warmup: load the deferred nexus tools"
prompt "List the names of your mcp__plugin_conexus_nexus__ tools. Reply with just the count." "warmup"

say "PreToolUse (Bash): the close gate and the two routing rules"
prompt "Run this exact bash command and show me its output: echo shakeout-bash-ok" "Bash turn"

say "PostToolUse (Write): the divergence-language guard"
prompt "Create a file /home/nexus/repo/shakeout_note.md containing exactly: shakeout wrote this" "Write turn"

say "PreToolUse (mcp tool): auto-approve, and a real tool round-trip"
prompt "Call mcp__plugin_conexus_nexus__scratch with action=put, content='shakeout scratch', tags='shakeout'. Then say DONE." "MCP tool turn"

say "SubagentStart/SubagentStop + the RDR-184 EXPECT writer"
prompt "Use the Agent tool to dispatch one general-purpose subagent whose entire task is to reply with the word PONG. Then tell me what it said." "subagent turn"

say "PostCompact"
T send-keys -t S "/compact" Enter
sleep 45
ok "/compact issued"

say "SessionEnd"
T send-keys -t S "/exit" Enter
sleep 20
ok "/exit issued"

# --- the census -----------------------------------------------------------
say "census: every declared handler against what fired"
python3 "$HOME_DIR/hook_census.py" "$PLUGIN/hooks/hooks.json" \
    "$RUN/pane.log" "$RUN/nx-mcp.log"
CENSUS=$?

say "RDR-184 ledger (the subagent family's own record)"
if nx-hook expectations_census "$SID_OF" > "$RUN/census.txt" 2>&1; then
    ok "expectations_census ran"
else
    printf '  note  expectations_census exit %s\n' "$?"
fi
sed -n '1,12p' "$RUN/census.txt" 2>/dev/null | sed 's/^/    /'

say "summary: $PASS passed, $FAIL failed"
if [ "$FAIL" -eq 0 ] && [ "$CENSUS" -eq 0 ]; then
    echo "HOOK-SURFACE SHAKEOUT PASSED"
    exit 0
fi
echo "HOOK-SURFACE SHAKEOUT FAILED (drive failures=$FAIL, census rc=$CENSUS)"
exit 1
