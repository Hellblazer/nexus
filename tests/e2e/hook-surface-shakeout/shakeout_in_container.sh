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

collect() {  # everything that might carry evidence, out to the mounted dir
    [ -d /artifacts ] || return 0
    cp -R "$RUN" /artifacts/run 2>/dev/null
    cp -R "$HOME_DIR/.claude" /artifacts/dot-claude 2>/dev/null
    cp -R "$HOME_DIR/.config/nexus" /artifacts/config-nexus 2>/dev/null
    cp "$HOME_DIR/.claude.json" /artifacts/ 2>/dev/null
    chmod -R a+rX /artifacts 2>/dev/null
    true
}
trap collect EXIT

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
# The sentinel is found by GLOB, not by session id. turn_end.py names its file
# after the session in its own stdin payload, so asking for a specific id
# means resolving that id FIRST -- and the first cut of this script resolved
# it from the wrong directory (`~/.config/nexus/status`; the record actually
# lives under the transcript workspace, as rdr208-mvv's STATUS_D shows). The
# id came back empty, every check then looked for a file literally named
# "turn-end.", and five turns timed out at 240 s each while the session was
# doing the work perfectly well. Only one session runs here, so the newest
# sentinel of any name is unambiguous and needs nothing resolved.
turn_stamp() {
    local newest=0 f t
    for f in "$RUN"/turn-end.*; do
        [ -e "$f" ] || continue
        t="$(stat -c %Y "$f" 2>/dev/null || echo 0)"
        [ "$t" -gt "$newest" ] && newest="$t"
    done
    echo "$newest"
}
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
# NO --mcp-config, and that is the point. The twelve tool-tier hooks address
# `plugin:conexus:nexus`, which is how Claude Code namespaces the PLUGIN's own
# .mcp.json key `nexus`. Passing our own --mcp-config (with
# --strict-mcp-config, which suppresses every other source) registered a
# server called plain `nexus` and left the tool tier addressing a name that
# did not exist -- measured as "Stop hook error: MCP server
# 'plugin:conexus:nexus' not connected", with the session continuing anyway.
# rdr208-mvv can use --mcp-config because the two hooks it tests are
# command-tier and never name a server. This one cannot.
CMD="export PATH=$HOME_DIR/nxenv/bin:$HOME_DIR/.local/bin:\$PATH && cd $WORK && exec claude --debug --dangerously-skip-permissions --plugin-dir $PLUGIN"
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

# Whether the SessionStart battery ran is answered by the CENSUS at the end,
# against the pane, not by hunting for one hook's private artefact here. The
# first cut asserted on a status record at a guessed path, got an empty
# result, and reported "the SessionStart battery did not run" -- a confident
# false negative about the product caused entirely by the harness looking in
# the wrong place. An assertion that can only be read one way when it fails
# is worse than no assertion, and the census already covers this claim with a
# denominator it derives from the shipped manifest.
#
# The session id is taken from the sentinel turn_end.py writes, once a turn
# has actually ended, because that file is named by the id the HOOK saw --
# no second source to disagree with.
sid_from_sentinel() {
    local f
    for f in "$RUN"/turn-end.*; do
        [ -e "$f" ] || continue
        basename "$f" | sed 's/^turn-end\.//'
        return 0
    done
    return 1
}

# --- the turns, each aimed at one event -----------------------------------
say "warmup: load the deferred nexus tools"
prompt "List the names of your mcp__plugin_conexus_nexus__ tools. Reply with just the count." "warmup"

# PROBE MODE: one turn, then dump everything and stop. Exists because the
# question "where is a hook invocation actually observable" cost a full
# six-turn billed run to ask badly. One turn is enough to answer it.
if [ -n "${SHAKEOUT_PROBE:-}" ]; then
    say "probe: one turn done, collecting every candidate evidence surface"
    collect
    echo "  files with any content under the run dir:"
    find "$RUN" "$HOME_DIR/.claude" "$HOME_DIR/.config/nexus" -type f -size +0 2>/dev/null \
        | head -40 | while read -r f; do printf '    %-64s %s\n' "$f" "$(wc -c < "$f")"; done
    echo "  any hook_ or nx-hook mention, by file:"
    grep -rlE 'hook_[a-z_]+|nx-hook' "$RUN" "$HOME_DIR/.claude" "$HOME_DIR/.config/nexus" 2>/dev/null \
        | head -20 | sed 's/^/    /'
    echo "PROBE COMPLETE"
    exit 0
fi

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
    "$RUN/pane.log"
CENSUS=$?

say "RDR-184 ledger (the subagent family's own record)"
SID_OF="$(sid_from_sentinel || true)"
if [ -z "$SID_OF" ]; then
    printf '  note  no turn-end sentinel, so no session id: the ledger check\n'
    printf '        below is UNRUN, not clean.\n'
fi
if [ -n "$SID_OF" ] && nx-hook expectations_census "$SID_OF" > "$RUN/census.txt" 2>&1; then
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
