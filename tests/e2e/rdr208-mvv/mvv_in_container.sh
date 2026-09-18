#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-208 local-mode MVV, the in-container journey (beads nexus-galkv.19,
# nexus-kdxyv: re-pointed from the deleted CLI watcher at the channel waiter).
#
# A virgin local-mode box: `nx init` provisions the bundled engine and PG.
# REAL Claude Code sessions run here, driven through tmux on a private socket
# and launched with the development-channel flag, so each session's own
# nx-mcp pushes mailbox references through the Claude Code channel (RDR-211,
# RDR-213). Under them run the REAL SessionStart and UserPromptSubmit hooks
# of the plugin under test (--plugin-dir /home/nexus/plugin, staged by run.sh
# from conexus/hooks) and the REAL mailbox_send tool. The sessions are real
# and billed. Nothing is stood in for.
#
# "Delivered over the channel" (never the pane alone): with NO prompt sent
# by this harness, the pane shows a new wake line for the session's mailbox,
# the session's channel-status record shows `announced` advanced, the
# session transcript carries exactly one UserPromptSubmit hook_success
# attachment rendering the correlation id (the drain hook claimed, acked and
# rendered the body at that wake), and the mailbox is empty afterwards.
# Default senders are proven the same way: the hook renders `from=<id>` from
# the row's own dims. Model-driven steps end on a literal token; every effect
# is read from the engine, a record, or the transcript, never the model's prose.
#
# EXPECT_BRANCH_FIX=1 (the plugin's SessionStart matcher names `fork`): step
# 6 asserts /branch hands the MCP server off to the fork: the session marker
# moves, the parent's directory entry is released, the parent's mail stays in
# the parent's mailbox and is never pushed into the fork. 0 asserts the
# pre-fix behaviour reproduces: no hook fires on /branch, the marker still
# names the parent, the parent's reference is pushed into the fork.
set -uo pipefail

EXPECT_BRANCH_FIX="${EXPECT_BRANCH_FIX:-1}"
MVV_LABEL="${MVV_LABEL:-unlabelled}"
export RUN="$HOME/run"
NXPY="$HOME/nxenv/bin/python"
TW="$HOME/.config/nexus/tuple-watch"
STATUS_D="$TW/channel-status.d"
WORK="$HOME/work"
PLUGIN="$HOME/plugin"
SOCK=rdr208
mkdir -p "$RUN" "$WORK"
unset NX_SESSION_ID CLAUDE_CODE_SESSION_ID CLAUDECODE CLAUDE_CODE_ENTRYPOINT ANTHROPIC_API_KEY
[ -f "$HOME/seed/claude.json" ] && cp "$HOME/seed/claude.json" "$HOME/.claude.json"

# Every tmux call goes through this wrapper: a private socket, never the
# default server (tests/test_rdr208_mvv_wiring.py pins it).
T() { command tmux -L "$SOCK" "$@"; }

PASS=0
FAIL=0
SEQ=0
say() { printf '\n== %s\n' "$*"; }
ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$*"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$*"; }
check() { local what="$1"; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }
now() { date +%s; }
tok() { SEQ=$((SEQ + 1)); printf '%s-%s' "$1" "$SEQ"; }
# NOTE, and the reason the conditions above are FUNCTIONS rather than an
# inline `test "$(...)"`: `wait_for 30 test "$(cmd)" = 0` expands the command
# substitution ONCE, before wait_for is ever called, so it polls a CONSTANT --
# it either passes immediately or burns the whole timeout. Every condition
# passed to wait_for must be a function name it can re-invoke. Measured
# 2026-09-18, three sites.
wait_for() {  # SECONDS CMD... : poll until CMD succeeds
    local deadline=$(( $(now) + $1 )); shift
    until "$@"; do [ "$(now)" -ge "$deadline" ] && return 1; sleep 1; done
}

# ── sessions ─────────────────────────────────────────────────────────────────
declare -A SID_OF=() PID_OF=() ANN=() WAKES=()

pane() { T capture-pane -p -J -S -400 -t "$1" 2>/dev/null; }
# Reply detection reads the TRANSCRIPT, never the pane. The pane carries the
# prompt's own echo, and the prompt names the token it asks for, so a bare
# pane grep passes before the model has answered; anchoring on the reply
# bullet instead made it depend on how this Claude Code version renders a
# turn, which cost a billed run on 2026-09-18 (the model HAD replied
# LOADED-1; the bullet was not on that line). An assistant message in the
# JSONL is unambiguous and version-stable.
reply_has() {  # NAME TOKEN: an ASSISTANT message carrying TOKEN
    local f; f="$(transcript_of "$1")"
    [ -n "$f" ] || return 1
    "$NXPY" "$HOME/assistant_said.py" "$f" "$2"
}
mailbox_empty() { [ "$(available "$1")" = 0 ]; }
rendered_once() { [ "$(rendered_count "$1" "$2")" = 1 ]; }
has_transcript() { [ -n "$(transcript_of "$1")" ]; }
status_field() { jq -r ".$2" "$STATUS_D/$1" 2>/dev/null; }
status_is() { [ "$(status_field "$1" "$2")" = "$3" ]; }
marker_of() { cat "$TW/session.${PID_OF[$1]}" 2>/dev/null; }
marker_names() { [ "$(marker_of "$1")" = "$2" ]; }
transcript_of() {
    local hits; hits="$(ls "$HOME/.claude/projects"/*/"${SID_OF[$1]}.jsonl" 2>/dev/null)"
    printf '%s' "${hits%%$'\n'*}"
}
transcripts() { ls "$HOME/.claude/projects"/*/*.jsonl 2>/dev/null | sort; }
wake_count() {  # NAME -> CHANNEL-delivered wakes for its own mailbox
    # From the transcript's `origin.kind == "channel"`, never the pane: the
    # pane truncates the wake line mid-session-id ("subspace mailbox/6a70b05f-
    # 1e35-4d1f-...") so the id can never match there, and a typed prompt
    # mentioning the same mailbox would match if it did. Measured 2026-09-18.
    local f; f="$(transcript_of "$1")"
    [ -n "$f" ] || { echo 0; return; }
    "$NXPY" "$HOME/channel_wakes.py" "$f" "${SID_OF[$1]}"
}
snap() { ANN[$1]="$(status_field "${SID_OF[$1]}" announced)"; WAKES[$1]="$(wake_count "$1")"; }
rendered_count() {  # NAME CORR -> UserPromptSubmit hook_success attachments rendering CORR
    local f; f="$(transcript_of "$1")"
    [ -n "$f" ] || { echo 0; return; }
    "$NXPY" - "$f" "$2" <<'PY'
import json, sys
n = 0
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        d = json.loads(line)
    except ValueError:
        continue
    a = d.get("attachment") or {}
    if d.get("type") == "attachment" and a.get("type") == "hook_success" \
            and a.get("hookEvent") == "UserPromptSubmit" \
            and f"correlation_id={sys.argv[2]} address=mailbox/" in (a.get("stdout") or ""):
        n += 1
print(n)
PY
}
rendered_from() {  # NAME CORR -> the from= the hook rendered for CORR
    local f; f="$(transcript_of "$1")"
    local hits; hits="$(grep -o "from=[^ ]* kind=[^ ]* correlation_id=$2 address" "$f" 2>/dev/null)"
    [[ "${hits%%$'\n'*}" =~ ^from=([^ ]+) ]] && printf '%s' "${BASH_REMATCH[1]}"
}

turn_stamp() {  # NAME -> the mtime of its turn-end sentinel, 0 when absent
    local f="$RUN/turn-end.${SID_OF[$1]}"
    [ -f "$f" ] && stat -c %Y "$f" 2>/dev/null || echo 0
}
turn_ended_since() { [ "$(turn_stamp "$1")" -gt "$2" ]; }
prompt() {  # NAME TEXT TOKEN: paste TEXT, Enter, wait for the TURN to end, assert TOKEN
    local before; before="$(turn_stamp "$1")"
    printf '%s' "$2" | T load-buffer -
    T paste-buffer -t "$1"
    sleep 0.5
    T send-keys -t "$1" Enter
    # The Stop hook says the turn ENDED (recording-rig's sentinel pattern);
    # only then is the transcript asked whether the model said the token. A
    # token poll alone cannot tell "still thinking" from "answered something
    # else", and the pane cannot tell either.
    if ! wait_for 180 turn_ended_since "$1" "$before"; then
        echo "  TIMEOUT: no turn-end from $1 within 180 s (is the Stop hook firing?)"
        pane "$1" | tail -15
        return 1
    fi
    if ! wait_for 20 reply_has "$1" "$3"; then
        echo "  TIMEOUT waiting for $3 in $1 (pane tail, then the transcript tail):"
        pane "$1" | tail -15
        local f; f="$(transcript_of "$1")"
        [ -n "$f" ] && tail -3 "$f" | cut -c1-400
        return 1
    fi
}
_mcp_json() {  # NAME -> path
    local f="$RUN/mcp-$1.json"
    printf '{"mcpServers":{"nexus":{"type":"stdio","command":"%s/nxenv/bin/nx-mcp","args":[],"env":{"NX_MCP_LOG":"%s/nx-mcp-%s.log"}}}}\n' \
        "$HOME" "$RUN" "$1" > "$f"
    printf '%s' "$f"
}
new_records_since() {  # LISTING -> status records not in LISTING
    comm -13 <(printf '%s\n' "$1") <(ls -1 "$STATUS_D" 2>/dev/null | sort) | grep -v '\.tmp$' || true
}
launch() {  # NAME [SID]: one real Claude Code session; with SID, `--resume SID`
    local name="$1" resume="${2:-}" mcp before t0
    mcp="$(_mcp_json "$name")"
    before="$(ls -1 "$STATUS_D" 2>/dev/null | sort)"
    t0="$(now)"
    # `exec`: the pane's process IS claude, so pane_pid is the claude pid the
    # hooks resolve through nexus.session.find_immediate_claude_pid.
    # PATH explicitly: tmux opens a login shell, which rebuilds PATH from the
    # profile and drops the image's own ENV PATH, so neither `nx` nor the venv
    # is on it. The hooks carry absolute paths for the same reason (run.sh),
    # this is the belt to that suspenders.
    local cmd="export PATH=$HOME/nxenv/bin:$HOME/.local/bin:\$PATH && cd $WORK && exec claude --dangerously-skip-permissions --plugin-dir $PLUGIN --mcp-config $mcp --strict-mcp-config --dangerously-load-development-channels server:nexus"
    [ -n "$resume" ] && cmd="$cmd --resume $resume"
    T kill-session -t "$name" 2>/dev/null
    T new-session -d -s "$name" -x 240 -y 50 || { bad "launch $name: tmux"; return 1; }
    T send-keys -t "$name" "$cmd" Enter
    # Each dialog is answered only when its own anchored text is on screen; a
    # dialog that never appears (trust is pre-seeded) is simply never answered.
    local deadline=$(( $(now) + 120 )) p
    while [ "$(now)" -lt "$deadline" ]; do
        p="$(pane "$name")"
        if grep -qF "bypass permissions on" <<<"$p"; then break
        elif grep -qF "I am using this for local development" <<<"$p"; then T send-keys -t "$name" Enter; sleep 3
        elif grep -qiE "trust this folder|project you trust" <<<"$p"; then T send-keys -t "$name" Enter; sleep 2
        elif grep -qF "Yes, I accept" <<<"$p"; then T send-keys -t "$name" Down; sleep 0.5; T send-keys -t "$name" Enter; sleep 5
        elif grep -qiE "oauth/authorize|Paste code|Login expired|Not logged in" <<<"$p"; then
            bad "launch $name: the session is not logged in"; pane "$name" | tail -15; return 1
        fi
        sleep 1
    done
    grep -qF "bypass permissions on" <<<"$(pane "$name")" \
        || { bad "launch $name: no prompt within 120 s"; pane "$name" | tail -25; return 1; }
    PID_OF[$name]="$(T display-message -p -t "$name" '#{pane_pid}')"
    local sid=""
    if [ -n "$resume" ]; then
        rewritten() { [ -f "$STATUS_D/$resume" ] && [ "$(stat -c %Y "$STATUS_D/$resume")" -ge "$t0" ]; }
        wait_for 90 rewritten || { bad "launch $name: status record for resumed $resume not rewritten within 90 s"; pane "$name" | tail -20; return 1; }
        sid="$resume"
    else
        one_new() { [ -n "$(new_records_since "$before")" ]; }
        wait_for 90 one_new || { bad "launch $name: no channel-status record within 90 s"; pane "$name" | tail -20; return 1; }
        local n; n="$(new_records_since "$before" | wc -l | tr -d ' ')"
        [ "$n" = 1 ] || { bad "launch $name: $n new status records, expected exactly one (launches are serialized)"; return 1; }
        sid="$(new_records_since "$before")"
    fi
    SID_OF[$name]="$sid"
    echo "  session $name: id $sid, claude pid ${PID_OF[$name]}"
    check "launch $name: waiter alive under its status record" wait_for 30 status_is "$sid" alive true
    check "  the SessionStart hook ran and its marker names this session" wait_for 60 marker_names "$name" "$sid"
    snap "$name"
    # Warmup turn: the MCP tools are deferred until ToolSearch loads them
    # (tests/cc-validation/README.md, "Deferred MCP tools").
    local t; t="$(tok LOADED)"
    check "  warmup: the deferred nexus tools are loaded" prompt "$name" \
        "Call ToolSearch with query \"select:mcp__plugin_conexus_nexus__tuple_subscribe,mcp__plugin_conexus_nexus__mailbox_send,mcp__plugin_conexus_nexus__tuple_in\" and then reply with exactly $t and nothing else." "$t"
    # The transcript file appears at the FIRST user message, not at startup,
    # so it is asserted AFTER the warmup turn, never before it.
    check "  the session transcript exists once the first turn has run" has_transcript "$name"
}
stop() {  # NAME: /exit, a plain process exit (releases nothing, as Claude Code quitting does)
    T send-keys -t "$1" "/exit" Enter
    exited() { ! kill -0 "${PID_OF[$1]}" 2>/dev/null; }
    wait_for 30 exited "$1" || { kill -TERM "${PID_OF[$1]}" 2>/dev/null; sleep 1; }
    T kill-session -t "$1" 2>/dev/null
}
arm() {  # NAME INSTANCE: the session subscribes its OWN instance-name mailbox
    # The prompt STATES whose name this is. `tuple_subscribe` accepts board
    # topics and this session's own instance-name mailbox, and refuses any
    # other session's, so a bare "subscribe mailbox/alpha-e6" leaves the model
    # to guess whether alpha-e6 is its own name. In the third billed run it
    # guessed not, declined, and asked which of two alternatives was meant
    # (2026-09-18) -- correctly, on the information it had. In production
    # that name comes from ListAgents; here the harness assigns it, so the
    # harness says so. The effect is still asserted from the engine (the
    # directory resolves to this session), never from the model's words.
    local t; t="$(tok DONE-ARM)"
    prompt "$1" "This session's own instance name is $2: that is the name other sessions address this session by, the way a ListAgents row would give it. Call the nexus MCP tool tuple_subscribe with subspace \"mailbox/$2\", which is this session's own instance-name mailbox and is exactly what that tool accepts. Then reply with exactly $t and nothing else." "$t"
}
model_send() {  # NAME TO CORR: a send with the DEFAULT sender, from inside the session
    local t; t="$(tok DONE-SEND)"
    prompt "$1" "Call the nexus MCP tool mailbox_send with to=\"$2\", body=\"rdr-208 local-mode mvv $3\", kind=\"notice\", correlation_id=\"$3\" and NO from_address, then reply with exactly $t and nothing else." "$t"
}
send() {  # TO CORR FROM -> result JSON (a harness send, explicit sender)
    "$NXPY" "$HOME/send.py" "$1" "$2" "rdr-208 local-mode mvv $2" "$3" 2>> "$RUN/send.err"
}
delivered() {  # NAME CORR: channel-delivered to NAME, with no prompt from this harness
    local name="$1" corr="$2" sid="${SID_OF[$1]}"
    woke() { [ "$(wake_count "$name")" -gt "${WAKES[$name]}" ]; }
    announced() { [ "$(status_field "$sid" announced)" -gt "${ANN[$name]}" ] 2>/dev/null; }
    rendered() { [ "$(rendered_count "$name" "$corr")" = 1 ]; }
    check "  $name woke on the reference (a new wake line, no prompt sent)" wait_for 60 woke
    check "  its status record's announced count advanced" wait_for 30 announced
    check "  the drain hook rendered $corr exactly once at that wake" wait_for 60 rendered
    check "  mailbox/$sid is empty afterwards (claimed and acked at the wake)" wait_for 30 mailbox_empty "$sid"
    snap "$name"
}
delivered_by_floor() {  # NAME CORR: the drain-hook FLOOR, at the session's next prompt
    # Used where the CHANNEL cannot be asserted inside a sane window: the
    # engine paces re-announcement per mailbox at DEFAULT_REANNOUNCE_INTERVAL_S
    # (150 s, src/nexus/mcp/channel.py), so a row written to a session that
    # was announced to moments ago waits out the rest of that window. A
    # resumed session is exactly that case, since the pre-resume process was
    # announced to on the same mailbox. Measured and reproduced without
    # Claude Code at all (T2 nexus/finding-resumed-session-waiter-does-not-
    # announce-2026-09-18): the second waiter DOES announce, just later. The
    # floor is unconditional and is what the design promises here, so this
    # asserts the floor and says so, rather than waiting out the interval in
    # every future run or pretending the channel missed.
    local t; t="$(tok OK)"
    prompt "$1" "Reply with exactly $t and nothing else." "$t" || bad "  prompt for the floor drain of $2"
    check "  the drain hook delivered $2 at the next prompt (the floor, not the channel)" \
        wait_for 30 rendered_once "$1" "$2"
    check "  mailbox/${SID_OF[$1]} is empty afterwards" wait_for 30 mailbox_empty "${SID_OF[$1]}"
    snap "$1"
}
not_delivered() {  # NAME CORR SECONDS: never rendered and no new wake within SECONDS
    sleep "$3"
    [ "$(rendered_count "$1" "$2")" = 0 ] && [ "$(wake_count "$1")" -eq "${WAKES[$1]}" ]
}
_save_artifacts() {
    [ -n "${MVV_ARTIFACTS:-}" ] && [ -d "$MVV_ARTIFACTS" ] || return 0
    local n
    for n in "${!PID_OF[@]}"; do pane "$n" > "$MVV_ARTIFACTS/pane.$n.txt" 2>/dev/null; done
    find "$RUN" -maxdepth 1 -type f -exec cp {} "$MVV_ARTIFACTS/" \;
    [ -d "$TW" ] && cp -R "$TW" "$MVV_ARTIFACTS/tuple-watch"
    [ -d "$HOME/.claude/projects" ] && cp -R "$HOME/.claude/projects" "$MVV_ARTIFACTS/transcripts"
    T kill-server 2>/dev/null
    return 0
}
trap _save_artifacts EXIT

# ── engine reads ──────────────────────────────────────────────────────────────
dir_json() { nx tuple directory "$1" --json 2>/dev/null; }
resolved() { dir_json "$1" | jq -r '.resolved_session_id // empty'; }
entries() { dir_json "$1" | jq '.entries | length'; }
available() { nx tuple stats "mailbox/$1" --json 2>/dev/null | jq '.available'; }
total_rows() { nx tuple stats "mailbox/$1" --json 2>/dev/null | jq '.total'; }
resolves_to() { [ "$(resolved "$1")" = "$2" ]; }
no_entries() { [ "$(entries "$1")" = "0" ]; }
new_transcript_since() {  # LISTING -> the one transcript not in LISTING, as a session id
    local new; new="$(comm -13 <(printf '%s\n' "$1") <(transcripts))"
    new="${new%%$'\n'*}"
    new="${new##*/}"
    printf '%s' "${new%.jsonl}"
}

echo "RDR-208 local-mode MVV: label=$MVV_LABEL expect_branch_fix=$EXPECT_BRANCH_FIX (real sessions, billed)"
nx --version
claude --version

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

say "launch A and B (serialized), arm alpha-e6 and bravo-94"
launch A || { echo "RDR-208 LOCAL-MODE MVV FAILED ($MVV_LABEL): launch A"; exit 1; }
SA="${SID_OF[A]}"
arm A alpha-e6 || bad "arm A"
launch B || { echo "RDR-208 LOCAL-MODE MVV FAILED ($MVV_LABEL): launch B"; exit 1; }
SB="${SID_OF[B]}"
arm B bravo-94 || bad "arm B"
check "directory/alpha-e6 resolves to A's session" wait_for 30 resolves_to alpha-e6 "$SA"
check "directory/bravo-94 resolves to B's session" wait_for 30 resolves_to bravo-94 "$SB"

# ── step 1: name resolution, both directions ─────────────────────────────────
say "step 1: send by name, both directions"
model_send B alpha-e6 s1-b2a || bad "B's model-driven send"
delivered A s1-b2a
# CONSUMED, not total: the drain hook claims and acks at the wake, so a
# delivered row is gone from the live census by the time this runs.
consumed_rows() { nx tuple stats "mailbox/$1" --json 2>/dev/null | jq '.consumed'; }
check "  it landed in A's mailbox (the name resolved to A's session id)" test "$(consumed_rows "$SA")" -ge 1
check "  from = B's session id (the default sender, as the hook rendered it)" test "$(rendered_from A s1-b2a)" = "$SB"
r="$(send bravo-94 s1-a2b "$SA")"
check "A -> bravo-94 resolved to B's session id" test "$(jq -r .to <<<"$r")" = "$SB"
check "  address_kind=session" test "$(jq -r .address_kind <<<"$r")" = session
delivered B s1-a2b

# ── step 2: /resume renames the session, the id stays ────────────────────────
say "step 2: /resume (new process, same session id, new name)"
stop A
RESUME_T="$(now)"
launch A2 "$SA" || { echo "RDR-208 LOCAL-MODE MVV FAILED ($MVV_LABEL): resume"; exit 1; }
arm A2 alpha-fc || bad "arm A2 (a resumed session subscribes its NEW name)"
check "directory/alpha-fc resolves to the same session id" wait_for 30 resolves_to alpha-fc "$SA"
r="$(send alpha-fc s2-new-name "$SB")"
check "B -> alpha-fc resolved to A's unchanged session id" test "$(jq -r .to <<<"$r")" = "$SA"
echo "  (a resumed session is inside the engine's 150 s re-announce window for its own"
echo "   mailbox, so delivery here is asserted at the floor; see delivered_by_floor)"
delivered_by_floor A2 s2-new-name

# ── step 3a: the old name inside its TTL ─────────────────────────────────────
say "step 3a: old name inside its lease"
r="$(send alpha-e6 s3a-old-name "$SB")"
check "alpha-e6 still resolves inside its TTL (a plain exit releases nothing)" test "$(jq -r .to <<<"$r")" = "$SA"
delivered_by_floor A2 s3a-old-name

# ── step 4: /clear ───────────────────────────────────────────────────────────
say "step 4: /clear: the MCP server hands off, the lease is released, the cleared record drains once"
before_t="$(transcripts)"
T send-keys -t A2 "/clear" Enter
cleared_id() { SA_C="$(new_transcript_since "$before_t")"; [ -n "$SA_C" ]; }
check "the clear minted a new session id" wait_for 60 cleared_id
echo "  session A2: id $SA -> $SA_C"
check "cleared.<new id> written, naming the old id" wait_for 30 grep -qsx "$SA" "$TW/cleared.$SA_C"
check "the old waiter stopped (its record shows alive=false within 30 s)" wait_for 30 status_is "$SA" alive false
check "a waiter runs under the new id (alive=true)" wait_for 60 status_is "$SA_C" alive true
check "  the old instance lease is released (alpha-fc gone within 30 s, not left to the 300 s TTL)" wait_for 30 no_entries alpha-fc
SID_OF[A2]="$SA_C"; snap A2
send "$SA" s4-pending "$SB" > /dev/null
check "mail to the OLD id after the clear waits in mailbox/old (nothing pushes it)" test "$(available "$SA")" = 1
t="$(tok OK)"
prompt A2 "Reply with exactly $t and nothing else." "$t" || bad "first prompt after the clear"
check "the first prompt delivers it exactly once, through the cleared record" wait_for 30 rendered_once A2 s4-pending
check "  the record is deleted once the old mailbox is empty" wait_for 15 test ! -e "$TW/cleared.$SA_C"
check "  mailbox/old is empty" test "$(available "$SA")" = 0
send "$SA" s4-late "$SB" > /dev/null
t="$(tok OK)"
prompt A2 "Reply with exactly $t and nothing else." "$t" || bad "prompt after the late send"
check "mail to the old id after the record is gone stays stranded (accepted baseline)" test "$(available "$SA")" = 1
arm A2 alpha-fc || bad "re-arm A2"
check "re-armed: alpha-fc resolves to the new session id" wait_for 30 resolves_to alpha-fc "$SA_C"
model_send A2 bravo-94 s4-from-after-clear || bad "A2's model-driven send"
delivered B s4-from-after-clear
check "a send after the clear defaults its from to the new session id" test "$(rendered_from B s4-from-after-clear)" = "$SA_C"

# ── step 5: one name, two live sessions ──────────────────────────────────────
say "step 5: a name held by two sessions is refused"
launch C || { echo "RDR-208 LOCAL-MODE MVV FAILED ($MVV_LABEL): launch C"; exit 1; }
launch D || { echo "RDR-208 LOCAL-MODE MVV FAILED ($MVV_LABEL): launch D"; exit 1; }
SC="${SID_OF[C]}"; SD="${SID_OF[D]}"
arm C mvv-shared || bad "arm C"
arm D mvv-shared || bad "arm D"
held_by_two() { [ "$(dir_json mvv-shared | jq '.holders | length')" = 2 ]; }
check "directory/mvv-shared has two distinct holders" wait_for 30 held_by_two
before_c="$(total_rows "$SC")"; before_d="$(total_rows "$SD")"
r="$(send mvv-shared s5-shared "$SB")"
check "mailbox_send refuses the ambiguous name" grep -q "more than one session" <<<"$r"
names_both() { grep -q "$SC" <<<"$r" && grep -q "$SD" <<<"$r"; }
check "  and names both holders" names_both
check "  and writes nothing" test "$(total_rows "$SC")/$(total_rows "$SD")" = "$before_c/$before_d"
stop C; stop D

# ── step 6: /branch ──────────────────────────────────────────────────────────
say "step 6: /branch (a new id in the same process)"
before_t="$(transcripts)"
T send-keys -t A2 "/branch" Enter
fork_id() { SF="$(new_transcript_since "$before_t")"; [ -n "$SF" ]; }
check "the branch minted a new session id" wait_for 60 fork_id
echo "  session A2: fork $SF of $SA_C"
sleep 3
check "the fork writes no cleared record" test ! -e "$TW/cleared.$SF"
if [ "$EXPECT_BRANCH_FIX" = 1 ]; then
    check "the fork's SessionStart moves the marker to the fork" wait_for 30 marker_names A2 "$SF"
    check "the parent's waiter stopped (alive=false)" wait_for 30 status_is "$SA_C" alive false
    check "a waiter runs under the fork (alive=true)" wait_for 60 status_is "$SF" alive true
    check "  the parent's directory entry is released" wait_for 30 no_entries alpha-fc
    send "$SA_C" s6-parent "$SB" > /dev/null
    check "the parent's mail is NOT pushed into the fork (15 s, no wake, nothing rendered)" not_delivered A2 s6-parent 15
    SID_OF[A2]="$SF"
    check "  and it stays in the parent's mailbox" test "$(available "$SA_C")" = 1
else
    check "pre-fix behaviour reproduces: no hook fired, the marker still names the parent" marker_names A2 "$SA_C"
    check "pre-fix behaviour reproduces: alpha-fc still resolves to the parent" resolves_to alpha-fc "$SA_C"
    send "$SA_C" s6-parent "$SB" > /dev/null
    pushed() { [ "$(wake_count A2)" -gt "${WAKES[A2]}" ]; }
    check "pre-fix behaviour reproduces: the parent's reference is pushed into the fork" wait_for 30 pushed
fi

# ── step 3b: the old name after its lease lapses ─────────────────────────────
say "step 3b: old name after its TTL (plain exit at $(date -u -d "@$RESUME_T" +%H:%M:%SZ))"
wait_s=$(( RESUME_T + 300 + 20 - $(now) ))
[ "$wait_s" -gt 0 ] && { echo "  waiting ${wait_s}s for the 300 s lease to lapse"; sleep "$wait_s"; }
before="$(total_rows "$SA")"
r="$(send alpha-e6 s3b-lapsed "$SB")"
check "the lapsed name is refused, naming the name" grep -q "no live holder for name 'alpha-e6'" <<<"$r"
check "  and nothing is written" test "$(total_rows "$SA")" = "$before"

stop A2; stop B
say "summary: $PASS passed, $FAIL failed"
if [ "$FAIL" -eq 0 ]; then
    echo "RDR-208 LOCAL-MODE MVV PASSED ($MVV_LABEL, expect_branch_fix=$EXPECT_BRANCH_FIX, $PASS checks)"
    exit 0
fi
echo "RDR-208 LOCAL-MODE MVV FAILED ($MVV_LABEL, $FAIL of $((PASS + FAIL)) checks failed)"
exit 1
