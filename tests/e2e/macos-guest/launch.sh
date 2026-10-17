#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Run RDR-228 MVV row 1 (local, start at login, then a real reboot) in a fresh
# macOS guest on hellmini, from the laptop, with no human step (nexus-ppuwc).
#
#   tests/e2e/macos-guest/launch.sh [--out DIR] [--wheel PATH] [--permission-mode MODE] [--keep-guest]
#
# What it does, in order:
#   0 stage     a per-run directory on hellmini holds host.sh (removed at the end)
#   1 guard     refuse (exit 75) while a suite or engine build holds the lease in
#               the hellmini clone's git common dir, or a GitHub runner job runs;
#               refuse (exit 3) when tart, the clean image or the guest key is
#               missing
#   2 restore   clone the clean image (nexus-mac-clean) to nx-mac-run-<run id>,
#               boot it headless, wait for ssh
#   3 deliver   the repo's conexus/ plugin, row1.sh and (--wheel) a candidate wheel
#   4 pre       row1.sh pre: a claude -p turn answers the setup question "local,
#               start at login", the setup finishes, launchd runs com.nexus.service,
#               a second turn stores a probe note through the MCP server
#   5 reboot    sudo shutdown -r inside the guest; the boot time must change and the
#               same tart run must still own the VM (a stop is a failure)
#   6 post      row1.sh post: auto-login brought the session back, launchd started
#               the job on its own, nx doctor passes, the note is still found, and a
#               new Claude Code session finds it through MCP
#   7 collect   the guest's ~/nxmvv/out comes back to DIR/guest-out
#   end         stop and delete the per-run VM, remove the per-run host directory
#
# Exit 0 only when every check of both phases passed and the per-run VM is gone.
# --keep-guest leaves the per-run VM running for diagnosis (delete it with
# host.sh destroy). Without --wheel the guest's setup installs conexus from PyPI
# at whatever version the plugin asks for.
#
# Credentials: the automation token goes through claude_credentials.py run
# --remote (RDR-219). Its remote shell here is host.sh gssh, so the token rides
# ssh stdin from the laptop to hellmini and on to the guest, and lands only in
# the environment of row1.sh and the claude processes it starts. It is never in
# an argument, a file or a log. The guest is deleted at the end, which removes
# whatever the session's processes held.
# SC2029: remote command lines are built here on purpose; every value in them is
# a validated plain token.
# shellcheck disable=SC2029
set -uo pipefail

HOST="${NX_MAC_GUEST_HOST:-hellmini}"
BASE="${NX_MAC_GUEST_DIR:-/Volumes/Bulk/tart/nexus}"
GHOME=/Users/admin
for pair in "NX_MAC_GUEST_HOST:$HOST" "NX_MAC_GUEST_DIR:$BASE"; do
    [[ "${pair#*:}" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "launch.sh: ${pair%%:*} must be a plain token" >&2; exit 2; }
done

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

SSH_OPTS=(-o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=6)
say() { echo "launch.sh: $(date -u +%H:%M:%S) $*" | tee -a "$out/launch.log"; }
quiet() { grep -v -i -E 'post-quantum|store now, decrypt later|pq\.html' || true; }
h() { ssh "${SSH_OPTS[@]}" "$HOST" "$RD/host.sh $*" </dev/null 2>&1 | quiet; return "${PIPESTATUS[0]}"; }

rd_made=0; vm_made=0; result=1; ended=0
finish() {
    [[ $ended -eq 0 ]] || return 0
    ended=1
    trap ':' INT TERM HUP
    if [[ $vm_made -eq 1 ]]; then
        if [[ ! -d "$out/guest-out" ]]; then
            say "collecting the guest's ~/nxmvv/out"
            h gfetch "$RUN" "$GHOME/nxmvv/out" "$RD/guest-out" >>"$out/launch.log" 2>&1 \
                && scp "${SSH_OPTS[@]}" -q -r "$HOST:$RD/guest-out" "$out/" </dev/null 2>&1 | quiet
        fi
        scp "${SSH_OPTS[@]}" -q "$HOST:$BASE/logs/$RUN.run.log" "$out/tart-run.log" </dev/null 2>/dev/null || true
        if [[ $keep -eq 1 ]]; then
            say "--keep-guest: $RUN left running on $HOST (delete with: ssh $HOST $BASE/host.sh destroy $RUN)"
            ssh "${SSH_OPTS[@]}" "$HOST" "cp $RD/host.sh $BASE/host.sh" </dev/null || true
        else
            h destroy "$RUN" | tee -a "$out/launch.log"
            if [[ ${PIPESTATUS[0]} -ne 0 ]]; then say "WARNING: $RUN may still exist on $HOST"; result=1; fi
            ssh "${SSH_OPTS[@]}" "$HOST" "rm -f $BASE/logs/$RUN.run.log" </dev/null || true
        fi
    fi
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

say "run $RUN on $HOST, logs in $out"
ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p $BASE/runs && mkdir $RD" </dev/null || fail "cannot create $RD on $HOST"
rd_made=1
scp "${SSH_OPTS[@]}" -q "$here/host.sh" "$HOST:$RD/host.sh" </dev/null || fail "cannot copy host.sh"
h preflight | tee -a "$out/launch.log"; rc=${PIPESTATUS[0]}
[[ $rc -eq 0 ]] || { say "preflight refused (exit $rc)"; result=3; exit 3; }
h guard | tee -a "$out/launch.log"; rc=${PIPESTATUS[0]}
if [[ $rc -eq 75 ]]; then say "hellmini is busy (lease or CI job); not starting the guest"; result=75; exit 75; fi
[[ $rc -eq 0 ]] || fail "the guard could not run (exit $rc)"

say "restore: clone and boot"
vm_made=1
h start "$RUN" | tee -a "$out/launch.log"; [[ ${PIPESTATUS[0]} -eq 0 ]] || fail "start"
ip="$(h waitssh "$RUN" 600)" || fail "no ssh into the guest: $ip"
say "guest up at $ip"

say "deliver: plugin, row1.sh${wheel:+, wheel $(basename "$wheel")}"
tar -C "$repo" -czf - conexus \
    | ssh "${SSH_OPTS[@]}" "$HOST" "$RD/host.sh gssh $RUN 'rm -rf nxmvv && mkdir -p nxmvv/plugin nxmvv/dist nxmvv/out && tar -C nxmvv/plugin -xzf -'" \
    || fail "cannot deliver the plugin"
scp "${SSH_OPTS[@]}" -q "$here/row1.sh" "$HOST:$RD/row1.sh" </dev/null || fail "cannot copy row1.sh to $HOST"
h gscp "$RUN" "$RD/row1.sh" "$GHOME/nxmvv/row1.sh" || fail "cannot copy row1.sh into the guest"
if [[ -n "$wheel" ]]; then
    scp "${SSH_OPTS[@]}" -q "$wheel" "$HOST:$RD/" </dev/null || fail "cannot copy the wheel to $HOST"
    h gscp "$RUN" "$RD/$(basename "$wheel")" "$GHOME/nxmvv/dist/" || fail "cannot copy the wheel into the guest"
    h gssh "$RUN" "touch nxmvv/use-wheel" || fail "cannot mark the wheel"
fi
h gssh "$RUN" "'chmod +x nxmvv/row1.sh && ls nxmvv/plugin/conexus/.claude-plugin/plugin.json'" >/dev/null || fail "the plugin did not land in the guest"

phase() {  # phase pre|post
    say "$1: row1.sh $1 (permission mode $mode)"
    "$E2E_PYTHON" "$repo/tests/e2e/lib/claude_credentials.py" run --remote "$HOST" \
        --remote-shell "$RD/host.sh gssh $RUN bash -s --" -- "$GHOME/nxmvv/row1.sh" "$1" "$mode" 2>&1 \
        | quiet | tee "$out/$1.txt"
    return "${PIPESTATUS[0]}"
}

phase pre; pre_rc=$?
if [[ $pre_rc -eq 0 ]]; then
    say "reboot: from inside the guest"
    h reboot "$RUN" 900 | tee -a "$out/launch.log"; [[ ${PIPESTATUS[0]} -eq 0 ]] || fail "the reboot did not complete"
    phase post; post_rc=$?
else
    say "pre phase failed (exit $pre_rc); no reboot"
    post_rc=1
fi

say "collect"
h gfetch "$RUN" "$GHOME/nxmvv/out" "$RD/guest-out" >>"$out/launch.log" 2>&1 || say "WARNING: could not fetch the guest's out directory"
scp "${SSH_OPTS[@]}" -q -r "$HOST:$RD/guest-out" "$out/" </dev/null 2>&1 | quiet

passed="$(cat "$out"/guest-out/checks-*.txt 2>/dev/null | grep -c '^CHECK PASS')"
failed="$(cat "$out"/guest-out/checks-*.txt 2>/dev/null | grep -c '^CHECK FAIL')"
ran_post=0; [[ -f "$out/guest-out/checks-post.txt" ]] && ran_post=1
result=0
finish
if [[ $result -ne 0 ]]; then
    say "MACOS GUEST ROW 1 FAILED: the per-run VM was not removed (checks passed $passed, failed $failed; logs in $out)"
    exit 1
fi
if [[ $pre_rc -eq 0 && $post_rc -eq 0 && "$failed" -eq 0 && "$passed" -gt 0 && $ran_post -eq 1 ]]; then
    say "MACOS GUEST ROW 1 PASSED: rows run 1, checks passed $passed, failed $failed (logs in $out)"
    result=0
else
    say "MACOS GUEST ROW 1 FAILED: rows run 1, checks passed $passed, failed $failed, pre exit $pre_rc, post exit $post_rc (logs in $out)"
    result=1
fi
exit "$result"
