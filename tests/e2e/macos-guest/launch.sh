#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Run RDR-228 MVV row 1 (local, start at login, then a real reboot) in a fresh
# macOS guest on hellmini, from the laptop, with no human step per run (nexus-ppuwc);
# the one standing dependency is a signed-in nxmvv GUI session (exit 3 without it).
#
#   tests/e2e/macos-guest/launch.sh [--out DIR] [--wheel PATH] [--permission-mode MODE] [--keep-guest]
#
# What it does, in order:
#   0 stage     a per-run directory on hellmini holds host.sh (removed at the end)
#   1 preflight refuse (exit 3) when tart, the clean image, the guest key or its
#               pinned host key is missing, the files host.sh trusts are writable
#               by someone else, or the tart account (nxmvv) is not signed in
#   2 guard     refuse (exit 75) while a suite or engine build holds the lease in
#               the hellmini clone's git common dir (an unreadable lease counts as
#               held), a GitHub runner job runs, or a tart VM or tart run of ours
#               exists
#   3 hold      host.sh hold acquires the hellmini `suite` lease and keeps it, over
#               an ssh session whose stdin this script owns, for the whole run: a
#               suite started mid-run finds it held and fails closed, and the
#               runner overlap guard waits. The hold destroys the per-run VM
#               before it releases the lease; if the destroy fails the lease is still
#               released and the host-side deadman deletes the VM at its cap. A run that dies, or a laptop that goes
#               away, ends the hold (stdin closes) and with it the VM; a host-side
#               deadman deletes the VM at 90 minutes whatever else happens.
#   4 restore   clone the clean image (nexus-mac-clean) to nx-mac-run-<run id>,
#               boot it headless, wait for ssh (host key pinned, address checked)
#   5 deliver   the repo's conexus/ plugin, row1.sh and (--wheel) a candidate wheel
#   6 pre       row1.sh pre: a claude -p turn answers the setup question "local,
#               start at login", the setup finishes, launchd runs com.nexus.service,
#               a second turn stores a probe note through the MCP server
#   7 reboot    sudo shutdown -r inside the guest; the boot session uuid must change,
#               ssh must be seen down, and the same tart run must still own the VM
#   8 post      row1.sh post: auto-login brought the session back, launchd started
#               the job on its own (a new pid), nx doctor passes, the note is still
#               found, and a new Claude Code session finds it through MCP
#   9 collect   the guest's ~/nxmvv/out comes back to DIR/guest-out
#   end         stop and delete the per-run VM (retried), release the lease, remove
#               the per-run host directory
#
# Exit 0 only when exactly the 12 expected checks ran and passed, no `sk-ant-`
# string appears anywhere under DIR, and the per-run VM is gone. A TERM, INT or
# HUP ends the run promptly: the phase in flight is killed, and the cleanup above
# runs with those signals ignored. --keep-guest leaves the per-run VM running
# (and the lease held) for diagnosis, until the cap or `host.sh destroy`. Without
# --wheel the guest's setup installs conexus from PyPI at whatever version the
# plugin asks for.
#
# Credentials: the automation token goes through claude_credentials.py run
# --remote (RDR-219). Its remote shell here is host.sh gssh, so the token rides
# ssh stdin from the laptop to hellmini and on to the guest, and lands only in
# the environment of row1.sh and of the claude processes it starts. It is never in
# an argument, a file or a log. The guest is deleted at the end, which removes
# whatever the session's processes held.
# SC2029: remote command lines are built here on purpose; every value in them is
# a validated plain token.
# shellcheck disable=SC2029
set -uo pipefail

HOST="${NX_MAC_GUEST_HOST:-hellmini}"
BASE=/Volumes/Bulk/tart/nexus
# The remote directory is overridable only by the test harness (host.sh reads the
# same flag), so a stray variable cannot point a real run elsewhere.
[[ "${NX_MAC_GUEST_TEST:-}" == 1 ]] && BASE="${NX_MAC_GUEST_DIR:-$BASE}"
GHOME=/Users/admin
for pair in "NX_MAC_GUEST_HOST:$HOST" "NX_MAC_GUEST_DIR:$BASE"; do
    [[ "${pair#*:}" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "launch.sh: ${pair%%:*} must be a plain token" >&2; exit 2; }
done

# The checks a complete run prints, by name: 7 before the reboot, 5 after.
EXPECTED_CHECKS="clean_start setup_done setup_local_autostart launchagent_plist launchd_job_running_pre doctor_pre note_stored_pre login_session_back launchd_job_running_post doctor_post note_survives_reboot session_search_post"
EXPECTED_COUNT=12

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
# shellcheck source=../lib/python.sh
source "$repo/tests/e2e/lib/python.sh"
e2e_python_resolve || exit 2

usage() { echo "usage: launch.sh [--out DIR] [--wheel PATH] [--permission-mode MODE] [--keep-guest]" >&2; }
out=''; wheel=''; mode=auto; keep=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) [[ $# -ge 2 ]] || { usage; exit 2; }; out="$2"; shift 2 ;;
        --wheel) [[ $# -ge 2 ]] || { usage; exit 2; }; wheel="$2"; shift 2 ;;
        --permission-mode) [[ $# -ge 2 ]] || { usage; exit 2; }; mode="$2"; shift 2 ;;
        --keep-guest) keep=1; shift ;;
        *) usage; exit 2 ;;
    esac
done
[[ "$mode" =~ ^[A-Za-z]+$ ]] || { echo "launch.sh: --permission-mode must be a plain word" >&2; exit 2; }
if [[ -n "$wheel" ]]; then
    [[ -f "$wheel" ]] || { echo "launch.sh: no such wheel: $wheel" >&2; exit 2; }
    [[ "$(basename "$wheel")" =~ ^conexus-[A-Za-z0-9._+-]+\.whl$ ]] || { echo "launch.sh: $wheel is not a conexus wheel" >&2; exit 2; }
fi
[[ -d "$repo/conexus" ]] || { echo "launch.sh: no plugin directory at $repo/conexus" >&2; exit 2; }
"$E2E_PYTHON" "$repo/tests/e2e/lib/claude_credentials.py" status >/dev/null \
    || { echo "launch.sh: the automation token is absent or expired (claude_credentials.py status)" >&2; exit 3; }

RUN="nx-mac-run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RD="$BASE/runs/$RUN"
out="${out:-${TMPDIR:-/tmp}/$RUN}"
mkdir -p "$out" || { echo "launch.sh: cannot create $out" >&2; exit 2; }
[[ -z "$(ls -A "$out")" ]] || { echo "launch.sh: $out is not empty" >&2; exit 2; }

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=6)
say() { echo "launch.sh: $(date -u +%H:%M:%S) $*" | tee -a "$out/launch.log"; }
quiet() { grep -v -i -E 'post-quantum|store now, decrypt later|pq\.html' || true; }
h() { ssh "${SSH_OPTS[@]}" "$HOST" "$RD/host.sh $*" </dev/null 2>&1 | quiet; return "${PIPESTATUS[0]}"; }
hv() { h "$@" | tee -a "$out/launch.log"; return "${PIPESTATUS[0]}"; }

# A phase runs in the background and this shell `wait`s for it, so a TERM, INT or
# HUP is acted on at once (bash runs a trap only after a foreground command ends,
# which would be after a 25-minute turn). finish kills what is still running.
BG_PID=''
bgwait() {
    "$@" &
    BG_PID=$!
    wait "$BG_PID"
    local rc=$?
    BG_PID=''
    return "$rc"
}
tree_pids() { local k; echo "$1"; for k in $(pgrep -P "$1" 2>/dev/null); do tree_pids "$k"; done; }
kill_tree() {
    local pids p
    pids="$(tree_pids "$1")"
    for p in $pids; do kill -TERM "$p" 2>/dev/null; done
    sleep 1
    for p in $pids; do kill -KILL "$p" 2>/dev/null; done
}

rd_made=0; vm_made=0; hold_made=0; result=1; ended=0
HOLD_PID=''; fifo_dir=''

end_hold() {
    [[ $hold_made -eq 1 ]] || return 0
    exec 8>&-
    for _ in $(seq 1 90); do kill -0 "$HOLD_PID" 2>/dev/null || break; sleep 1; done
    if kill -0 "$HOLD_PID" 2>/dev/null; then
        say "WARNING: the hold session did not end; killing it"
        kill -TERM "$HOLD_PID" 2>/dev/null
    fi
    wait "$HOLD_PID" 2>/dev/null
    quiet <"$out/hold.log" >>"$out/launch.log"
    if grep 'HOLD: released the suite lease' "$out/hold.log" >/dev/null 2>&1; then
        say "the hellmini suite lease is released"
    else
        say "WARNING: the suite lease may still be held on $HOST (see hold.log)"; result=1
    fi
    [[ -z "$fifo_dir" ]] || rm -rf "$fifo_dir"
    hold_made=0
}

finish() {
    [[ $ended -eq 0 ]] || return 0
    ended=1
    # Ignored, not handled: the ssh, scp and tee started below inherit the
    # disposition, so a second Ctrl-C cannot kill the cleanup halfway.
    trap '' INT TERM HUP
    if [[ -n "$BG_PID" ]]; then kill_tree "$BG_PID"; BG_PID=''; fi
    if [[ $vm_made -eq 1 ]]; then
        if [[ ! -d "$out/guest-out" ]]; then
            say "collecting the guest's ~/nxmvv/out"
            h gfetch "$RUN" "$GHOME/nxmvv/out" "$RD/guest-out" >>"$out/launch.log" 2>&1 \
                && scp "${SSH_OPTS[@]}" -q -r "$HOST:$RD/guest-out" "$out/" </dev/null 2>&1 | quiet
        fi
        scp "${SSH_OPTS[@]}" -q "$HOST:$BASE/logs/$RUN.run.log" "$out/tart-run.log" </dev/null 2>/dev/null || true
        if [[ $keep -eq 1 ]]; then
            say "--keep-guest: $RUN left running on $HOST, lease held until it is deleted or 90 min (delete with: ssh $HOST $BASE/host.sh destroy $RUN)"
            # for the manual destroy named above; the hold itself destroys through its own private copy (nexus-2fn57)
            ssh "${SSH_OPTS[@]}" "$HOST" "cp $RD/host.sh $BASE/host.sh" </dev/null || true
            hold_made=0    # the hold outlives this script: it saw `keep`, and EOF on its stdin only starts its wait for the VM to go
            rm -rf "$fifo_dir"
        else
            destroyed=0
            for _ in 1 2; do
                if hv destroy "$RUN"; then destroyed=1; break; fi
                sleep 3
            done
            if [[ $destroyed -ne 1 ]]; then say "WARNING: $RUN may still exist on $HOST"; result=1; fi
            ssh "${SSH_OPTS[@]}" "$HOST" "rm -f $BASE/logs/$RUN.run.log $BASE/logs/$RUN.deadman.log" </dev/null || true
        fi
    fi
    end_hold
    if [[ $rd_made -eq 1 ]]; then
        ssh "${SSH_OPTS[@]}" "$HOST" "rm -rf $RD" </dev/null || say "WARNING: could not remove $RD on $HOST"
    fi
}
# shellcheck disable=SC2329  # runs from the EXIT trap
on_exit() { local rc=$?; trap - EXIT; finish; [[ $rc -ne 0 ]] || rc=$result; exit "$rc"; }
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
fail() { say "FAILED: $*"; result=1; exit 1; }

start_hold() {
    local keep_arg='' i
    [[ $keep -eq 1 ]] && keep_arg=' keep'
    fifo_dir="$(mktemp -d)" || fail "cannot make a temp directory"
    mkfifo "$fifo_dir/in" || fail "cannot make a fifo"
    ssh "${SSH_OPTS[@]}" "$HOST" "$RD/host.sh hold $RUN$keep_arg" <"$fifo_dir/in" >"$out/hold.log" 2>&1 &
    HOLD_PID=$!
    hold_made=1
    exec 8>"$fifo_dir/in"
    for _ in $(seq 1 90); do
        if grep '^HOLD: acquired the suite lease' "$out/hold.log" >/dev/null 2>&1; then return 0; fi
        kill -0 "$HOLD_PID" 2>/dev/null || break
        sleep 1
    done
    kill -0 "$HOLD_PID" 2>/dev/null && kill -TERM "$HOLD_PID" 2>/dev/null
    wait "$HOLD_PID"; local rc=$?
    hold_made=0; exec 8>&-
    if [[ $rc -eq 75 ]]; then say "hellmini is busy (the suite lease is held); not starting the guest"; result=75; exit 75; fi
    quiet <"$out/hold.log" >>"$out/launch.log"
    fail "the suite lease could not be taken (hold exit $rc)"
}

# shellcheck disable=SC2329  # runs through bgwait
deliver() {
    tar -C "$repo" -czf - conexus \
        | ssh "${SSH_OPTS[@]}" "$HOST" "$RD/host.sh gssh $RUN 'rm -rf nxmvv && mkdir -p nxmvv/plugin nxmvv/dist nxmvv/out && tar -C nxmvv/plugin -xzf -'" \
        || { say "cannot deliver the plugin"; return 1; }
    scp "${SSH_OPTS[@]}" -q "$here/row1.sh" "$HOST:$RD/row1.sh" </dev/null || { say "cannot copy row1.sh to $HOST"; return 1; }
    hv gscp "$RUN" "$RD/row1.sh" "$GHOME/nxmvv/row1.sh" || { say "cannot copy row1.sh into the guest"; return 1; }
    if [[ -n "$wheel" ]]; then
        scp "${SSH_OPTS[@]}" -q "$wheel" "$HOST:$RD/" </dev/null || { say "cannot copy the wheel to $HOST"; return 1; }
        hv gscp "$RUN" "$RD/$(basename "$wheel")" "$GHOME/nxmvv/dist/" || { say "cannot copy the wheel into the guest"; return 1; }
        hv gssh "$RUN" "touch nxmvv/use-wheel" || { say "cannot mark the wheel"; return 1; }
    fi
    h gssh "$RUN" "'chmod +x nxmvv/row1.sh && ls nxmvv/plugin/conexus/.claude-plugin/plugin.json'" >/dev/null \
        || { say "the plugin did not land in the guest"; return 1; }
}

# shellcheck disable=SC2329  # runs through bgwait
phase() {  # phase pre|post
    say "$1: row1.sh $1 (permission mode $mode)"
    "$E2E_PYTHON" "$repo/tests/e2e/lib/claude_credentials.py" run --remote "$HOST" \
        --remote-shell "$RD/host.sh gssh $RUN bash -s --" -- "$GHOME/nxmvv/row1.sh" "$1" "$mode" 2>&1 \
        | quiet | tee "$out/$1.txt"
    return "${PIPESTATUS[0]}"
}

# shellcheck disable=SC2329  # runs through bgwait
collect() {
    h gfetch "$RUN" "$GHOME/nxmvv/out" "$RD/guest-out" >>"$out/launch.log" 2>&1 || say "WARNING: could not fetch the guest's out directory"
    scp "${SSH_OPTS[@]}" -q -r "$HOST:$RD/guest-out" "$out/" </dev/null 2>&1 | quiet
}

say "run $RUN on $HOST, logs in $out"
ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p $BASE/runs && mkdir $RD" </dev/null || fail "cannot create $RD on $HOST"
rd_made=1
scp "${SSH_OPTS[@]}" -q "$here/host.sh" "$HOST:$RD/host.sh" </dev/null || fail "cannot copy host.sh"
hv preflight; rc=$?
[[ $rc -eq 0 ]] || { say "preflight refused (exit $rc)"; result=3; exit 3; }
hv guard; rc=$?
if [[ $rc -eq 75 ]]; then say "hellmini is busy (lease, CI job or a tart VM); not starting the guest"; result=75; exit 75; fi
[[ $rc -eq 0 ]] || fail "the guard could not run (exit $rc)"
start_hold

say "restore: clone and boot"
vm_made=1
hv start "$RUN" || fail "start"
bgwait hv waitssh "$RUN" 600 || fail "no ssh into the guest"

say "deliver: plugin, row1.sh${wheel:+, wheel $(basename "$wheel")}"
bgwait deliver || fail "deliver"

bgwait phase pre; pre_rc=$?
if [[ $pre_rc -eq 0 ]]; then
    say "reboot: from inside the guest"
    bgwait hv reboot "$RUN" 900 || fail "the reboot did not complete"
    bgwait phase post; post_rc=$?
else
    say "pre phase failed (exit $pre_rc); no reboot"
    post_rc=1
fi

say "collect"
bgwait collect

result=0
finish

# What came back: counts, the exact set of check names, and no token anywhere.
checks="$(cat "$out"/guest-out/checks-*.txt 2>/dev/null)"
passed="$(grep -c '^CHECK PASS' <<<"$checks")"
failed="$(grep -c '^CHECK FAIL' <<<"$checks")"
got="$(awk '/^CHECK (PASS|FAIL) /{n=$3; sub(/:$/,"",n); print n}' <<<"$checks" | sort | tr '\n' ' ')"
# shellcheck disable=SC2086  # the names are plain words, split on purpose
want="$(printf '%s\n' $EXPECTED_CHECKS | sort | tr '\n' ' ')"
names_ok=0; [[ "$got" == "$want" ]] && names_ok=1
leaks="$(grep -r -a -l 'sk-ant-' "$out" 2>/dev/null || true)"

if [[ $result -ne 0 ]]; then
    say "MACOS GUEST ROW 1 FAILED: the per-run VM was not removed or the lease was not released (checks passed $passed, failed $failed; logs in $out)"
    exit 1
fi
if [[ -n "$leaks" ]]; then
    say "MACOS GUEST ROW 1 FAILED: a Claude token string (sk-ant-) is in the collected output: $(echo "$leaks" | tr '\n' ' ')"
    exit 1
fi
if [[ $names_ok -ne 1 ]]; then
    say "MACOS GUEST ROW 1 FAILED: the checks that ran are not the $EXPECTED_COUNT expected ones. ran: [$got] expected: [$want] (logs in $out)"
    exit 1
fi
if [[ $pre_rc -eq 0 && $post_rc -eq 0 && "$failed" -eq 0 && "$passed" -eq $EXPECTED_COUNT ]]; then
    say "MACOS GUEST ROW 1 PASSED: rows run 1, checks passed $passed, failed $failed (logs in $out)"
    exit 0
fi
say "MACOS GUEST ROW 1 FAILED: rows run 1, checks passed $passed, failed $failed, pre exit $pre_rc, post exit $post_rc (logs in $out)"
exit 1
