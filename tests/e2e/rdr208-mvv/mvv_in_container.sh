#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-208 local-mode MVV, the in-container journey (bead nexus-galkv.19).
#
# A virgin local-mode box: `nx init` provisions the bundled engine and PG.
# Stand-in claude processes (session_server.sh) play two or more Claude Code
# sessions; under them run the REAL SessionStart hook, the REAL drain hook
# (conexus/hooks/scripts/mailbox_drain.py from the version under test), the
# REAL `nx tuple watch`, and the REAL mailbox_send tool function. Only Claude
# Code itself is stood in for, replaying what was measured live on
# 2026-09-14 (T2 nexus/rdr-208-mvv-2026-09-14): /clear fires SessionStart
# source=clear in the same process; /resume is a new process on the same
# session id; /branch mints a new id in the same process and fires no
# SessionStart.
#
# EXPECT_BRANCH_FIX=1 asserts step 6's fix (the parent's watcher stops and
# releases after /branch); 0 asserts the 7.46.0 defect reproduces.
set -uo pipefail

EXPECT_BRANCH_FIX="${EXPECT_BRANCH_FIX:-1}"
MVV_LABEL="${MVV_LABEL:-unlabelled}"
export RUN="$HOME/run"
HOOKS="$HOME/hooks"
NXPY="$HOME/nxenv/bin/python"
TW="$HOME/.config/nexus/tuple-watch"
mkdir -p "$RUN"
unset NX_SESSION_ID CLAUDE_CODE_SESSION_ID

PASS=0
FAIL=0
say() { printf '\n== %s\n' "$*"; }
ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$*"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$*"; }
check() { local what="$1"; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }
now() { date +%s; }
uuid() { cat /proc/sys/kernel/random/uuid; }

# ── stand-in sessions ────────────────────────────────────────────────────────
declare -A SID_OF=()
LAST_ID=""
# Unique per call without a shared counter: send() and drain() run inside
# $(...), a subshell, where a counter increment never reaches the parent and
# a reused id would read a stale .rc before the new command finished.
_new_id() { printf 'c%s-%s-%s' "$(date +%s%N)" "$BASHPID" "$RANDOM"; }
_send_cmd() {  # SERVER MODE CMD
    LAST_ID="$(_new_id)"
    printf '%s %s %s\n' "$LAST_ID" "$2" "$(printf '%s' "$3" | base64 -w0)" > "$RUN/$1.fifo"
    local waited=0
    until [ -f "$RUN/$LAST_ID.rc" ]; do
        sleep 0.2
        waited=$((waited + 1))
        [ "$waited" -gt 1500 ] && { echo "TIMEOUT: $LAST_ID on $1" >&2; return 99; }
    done
    return "$(cat "$RUN/$LAST_ID.rc")"
}
start_server() {  # NAME
    nohup "$HOME/bin/claude" "$HOME/session_server.sh" "$1" > "$RUN/$1.server.log" 2>&1 &
    local waited=0
    until [ -p "$RUN/$1.fifo" ] && [ -f "$RUN/$1.server.pid" ]; do
        sleep 0.1
        waited=$((waited + 1))
        [ "$waited" -gt 100 ] && { echo "server $1 never started" >&2; return 1; }
    done
}
stop_server() {  # NAME: a process exit, as when Claude Code quits
    local pid
    pid="$(cat "$RUN/$1.server.pid")"
    pkill -TERM -P "$pid" 2>/dev/null
    kill -TERM "$pid" 2>/dev/null
    sleep 1
}
alive() {  # PID: running, not a zombie
    local stat
    stat="$(ps -o stat= -p "$1" 2>/dev/null)" || return 1
    [ -n "$stat" ] && [[ "$stat" != Z* ]]
}
env_for() { printf 'unset NX_SESSION_ID; export CLAUDE_CODE_SESSION_ID=%q; ' "$1"; }
_payload() {  # KIND JSON -> path
    local f="$RUN/payload.$1.$(_new_id).json"
    printf '%s' "$2" > "$f"
    printf '%s' "$f"
}
# Every command's out/err/rc and the watch-state files leave the container on
# any exit, for diagnosis (run.sh mounts MVV_ARTIFACTS).
_save_artifacts() {
    [ -n "${MVV_ARTIFACTS:-}" ] && [ -d "$MVV_ARTIFACTS" ] || return 0
    find "$RUN" -maxdepth 1 -type f -exec cp {} "$MVV_ARTIFACTS/" \;
    [ -d "$TW" ] && cp -R "$TW" "$MVV_ARTIFACTS/tuple-watch"
    return 0
}
trap _save_artifacts EXIT
session_start() {  # SERVER SID SOURCE
    local f
    f="$(_payload ss "{\"session_id\":\"$2\",\"source\":\"$3\",\"hook_event_name\":\"SessionStart\"}")"
    SID_OF[$1]="$2"
    _send_cmd "$1" run "$(env_for "$2")nx hook session-start < $f"
}
branch() {  # SERVER NEW_SID: /branch mints an id and fires no SessionStart
    SID_OF[$1]="$2"
}
drain() {  # SERVER -> the hook's stdout (the injected context)
    local sid="${SID_OF[$1]}" f
    f="$(_payload dr "{\"session_id\":\"$sid\",\"hook_event_name\":\"UserPromptSubmit\",\"prompt\":\"mvv\",\"cwd\":\"$HOME\"}")"
    _send_cmd "$1" run "$(env_for "$sid")python3 $HOOKS/mailbox_drain.py < $f"
    cat "$RUN/$LAST_ID.out"
}
arm() {  # SERVER NAME -> WATCH_PID, WATCH_OUT
    _send_cmd "$1" spawn "$(env_for "${SID_OF[$1]}")exec nx tuple watch --instance $2"
    WATCH_PID="$(cat "$RUN/$LAST_ID.pid")"
    WATCH_OUT="$RUN/$LAST_ID.out"
}
send() {  # SERVER TO CORRELATION [FROM] -> result JSON
    _send_cmd "$1" run "$(env_for "${SID_OF[$1]}")$NXPY $HOME/send.py $2 $3 'rdr-208 local-mode mvv $3' ${4:-}"
    cat "$RUN/$LAST_ID.out"
}
count_delivered() {  # CORRELATION TEXT
    printf '%s' "$2" | grep -o "correlation_id=$1\b" | wc -l | tr -d ' '
}
dir_json() { nx tuple directory "$1" --json 2>/dev/null; }
resolved() { dir_json "$1" | jq -r '.resolved_session_id // empty'; }
entries() { dir_json "$1" | jq '.entries | length'; }
available() { nx tuple stats "mailbox/$1" --json 2>/dev/null | jq '.available'; }
total_rows() { nx tuple stats "mailbox/$1" --json 2>/dev/null | jq '.total'; }
marker_of() { cat "$TW/session.$(cat "$RUN/$1.server.pid")" 2>/dev/null; }
wait_for() {  # SECONDS CMD... : poll until CMD succeeds
    local deadline=$(( $(now) + $1 )); shift
    until "$@"; do [ "$(now)" -ge "$deadline" ] && return 1; sleep 1; done
}
resolves_to() { [ "$(resolved "$1")" = "$2" ]; }
no_entries() { [ "$(entries "$1")" = "0" ]; }
exited() { ! alive "$1"; }

echo "RDR-208 local-mode MVV: label=$MVV_LABEL expect_branch_fix=$EXPECT_BRANCH_FIX"
nx --version

# ── provision ────────────────────────────────────────────────────────────────
say "provision: nx init (local mode, virgin HOME)"
if nx init --yes --no-autostart > "$RUN/init.log" 2>&1; then
    ok "nx init"
else
    bad "nx init (see below)"; tail -30 "$RUN/init.log"
    echo "RDR-208 LOCAL-MODE MVV FAILED ($MVV_LABEL): provisioning"; exit 1
fi
templates="$(nx tuple templates --json 2>/dev/null)"
if [[ "$templates" == *directory/* ]]; then
    ok "the local engine carries the directory/<name> template"
else
    bad "no directory template on the local engine"; nx tuple templates 2>&1 | tail -20
fi

SA="$(uuid)"; SB="$(uuid)"
start_server A; start_server B
session_start A "$SA" startup; session_start B "$SB" startup
arm A alpha-e6; WA="$WATCH_PID"; WA_OUT="$WATCH_OUT"
arm B bravo-94; WB="$WATCH_PID"
drain A > /dev/null; drain B > /dev/null
check "directory/alpha-e6 resolves to A's session" wait_for 30 resolves_to alpha-e6 "$SA"
check "directory/bravo-94 resolves to B's session" wait_for 30 resolves_to bravo-94 "$SB"

# ── step 1: name resolution, both directions ─────────────────────────────────
say "step 1: send by name, both directions"
r="$(send B alpha-e6 s1-b2a)"
check "B -> alpha-e6 resolved to A's session id" test "$(jq -r .to <<<"$r")" = "$SA"
check "  address_kind=session" test "$(jq -r .address_kind <<<"$r")" = session
check "  from = B's session id (default sender)" test "$(jq -r .from <<<"$r")" = "$SB"
check "  A's watcher pinged it" wait_for 15 grep -q "s1-b2a" "$WA_OUT"
check "  A's drain delivered it exactly once" test "$(count_delivered s1-b2a "$(drain A)")" = 1
check "  mailbox/A is empty after the drain" test "$(available "$SA")" = 0
r="$(send A bravo-94 s1-a2b)"
check "A -> bravo-94 resolved to B's session id" test "$(jq -r .to <<<"$r")" = "$SB"
check "  B's drain delivered it exactly once" test "$(count_delivered s1-a2b "$(drain B)")" = 1

# ── step 2: /resume renames the session, the id stays ────────────────────────
say "step 2: /resume (new process, same session id, new name)"
stop_server A
RESUME_T="$(now)"
check "A's watcher stopped with its process" wait_for 10 exited "$WA"
start_server A2
session_start A2 "$SA" resume
arm A2 alpha-fc; WA2="$WATCH_PID"; WA2_OUT="$WATCH_OUT"
drain A2 > /dev/null
check "directory/alpha-fc resolves to the same session id" wait_for 30 resolves_to alpha-fc "$SA"
r="$(send B alpha-fc s2-new-name)"
check "B -> alpha-fc resolved to A's unchanged session id" test "$(jq -r .to <<<"$r")" = "$SA"
check "  delivered once" test "$(count_delivered s2-new-name "$(drain A2)")" = 1

# ── step 3a: the old name inside its TTL ─────────────────────────────────────
say "step 3a: old name inside its lease"
r="$(send B alpha-e6 s3a-old-name)"
check "alpha-e6 still resolves inside its TTL (a plain exit releases nothing)" test "$(jq -r .to <<<"$r")" = "$SA"
check "  delivered once" test "$(count_delivered s3a-old-name "$(drain A2)")" = 1

# ── step 4: /clear ───────────────────────────────────────────────────────────
say "step 4: /clear with mail pending, lease release, cleared record"
r="$(send B "$SA" s4-pending)"
check "mail pending at the clear is in mailbox/A" test "$(available "$SA")" = 1
SA_C="$(uuid)"
session_start A2 "$SA_C" clear
check "cleared.<new id> written, naming the old id" grep -qx "$SA" "$TW/cleared.$SA_C"
check "the old watcher self-stops" wait_for 15 exited "$WA2"
check "  its STOP line names the new session" grep -q "STOP: this conversation is now session $SA_C" "$WA2_OUT"
check "  its directory entry is released (gone within 5 s, not left to the 300 s TTL)" wait_for 5 no_entries alpha-fc
out="$(drain A2)"
check "the first prompt delivers the pending mail exactly once, through the record" test "$(count_delivered s4-pending "$out")" = 1
check "  the record is deleted once the old mailbox is empty" test ! -e "$TW/cleared.$SA_C"
check "  mailbox/old is empty" test "$(available "$SA")" = 0
send B "$SA" s4-late > /dev/null
drain A2 > /dev/null
check "mail to the old id after the record is gone stays stranded (accepted baseline)" test "$(available "$SA")" = 1
arm A2 alpha-fc; WA3="$WATCH_PID"; WA3_OUT="$WATCH_OUT"
check "re-armed: alpha-fc resolves to the new session id" wait_for 30 resolves_to alpha-fc "$SA_C"
r="$(send A2 bravo-94 s4-from-after-clear)"
check "a send after the clear defaults its from to the new session id" test "$(jq -r .from <<<"$r")" = "$SA_C"
drain B > /dev/null

# ── step 5: one name, two live sessions ──────────────────────────────────────
say "step 5: a name held by two sessions is refused"
SC="$(uuid)"; SD="$(uuid)"
start_server C; start_server D
session_start C "$SC" startup; session_start D "$SD" startup
arm C mvv-shared; WC="$WATCH_PID"
arm D mvv-shared; WD="$WATCH_PID"
held_by_two() { [ "$(dir_json mvv-shared | jq '.holders | length')" = 2 ]; }
check "directory/mvv-shared has two distinct holders" wait_for 30 held_by_two
before_c="$(total_rows "$SC")"; before_d="$(total_rows "$SD")"
r="$(send B mvv-shared s5-shared)"
check "mailbox_send refuses the ambiguous name" grep -q "more than one session" <<<"$r"
names_both() { grep -q "$SC" <<<"$r" && grep -q "$SD" <<<"$r"; }
check "  and names both holders" names_both
check "  and writes nothing" test "$(total_rows "$SC")/$(total_rows "$SD")" = "$before_c/$before_d"
stop_server C; stop_server D

# ── step 6: /branch ──────────────────────────────────────────────────────────
say "step 6: /branch (new id in the same process, no SessionStart)"
SF="$(uuid)"
branch A2 "$SF"
out="$(drain A2)"
check "the fork writes no cleared record" test ! -e "$TW/cleared.$SF"
send B "$SA_C" s6-parent > /dev/null
if [ "$EXPECT_BRANCH_FIX" = 1 ]; then
    check "the fork's first prompt moves the marker to the fork" test "$(marker_of A2)" = "$SF"
    check "the parent's watcher self-stops" wait_for 15 exited "$WA3"
    check "  its STOP line names the fork" grep -q "STOP: this conversation is now session $SF" "$WA3_OUT"
    check "  its directory entry is released" wait_for 5 no_entries alpha-fc
    check "the fork's first prompt re-arms (arm instruction printed)" grep -q "MAILBOX WATCH" <<<"$out"
else
    check "7.46.0 defect reproduces: the marker still names the parent" test "$(marker_of A2)" = "$SA_C"
    sleep 12
    check "7.46.0 defect reproduces: the parent's watcher is still running in the fork" alive "$WA3"
    check "7.46.0 defect reproduces: alpha-fc still resolves to the parent" resolves_to alpha-fc "$SA_C"
    check "7.46.0 defect reproduces: the parent's mail is pinged into the fork" wait_for 15 grep -q s6-parent "$WA3_OUT"
fi
drain A2 > /dev/null
check "the parent's mail stays in the parent's mailbox" test "$(available "$SA_C")" = 1

# ── step 3b: the old name after its lease lapses ─────────────────────────────
say "step 3b: old name after its TTL (plain exit at $(date -u -d "@$RESUME_T" +%H:%M:%SZ))"
wait_s=$(( RESUME_T + 300 + 20 - $(now) ))
[ "$wait_s" -gt 0 ] && { echo "  waiting ${wait_s}s for the 300 s lease to lapse"; sleep "$wait_s"; }
before="$(total_rows "$SA")"
r="$(send B alpha-e6 s3b-lapsed)"
check "the lapsed name is refused, naming the name" grep -q "no live holder for name 'alpha-e6'" <<<"$r"
check "  and nothing is written" test "$(total_rows "$SA")" = "$before"

stop_server A2; stop_server B
say "summary: $PASS passed, $FAIL failed"
if [ "$FAIL" -eq 0 ]; then
    echo "RDR-208 LOCAL-MODE MVV PASSED ($MVV_LABEL, expect_branch_fix=$EXPECT_BRANCH_FIX, $PASS checks)"
    exit 0
fi
echo "RDR-208 LOCAL-MODE MVV FAILED ($MVV_LABEL, $FAIL of $((PASS + FAIL)) checks failed)"
exit 1
