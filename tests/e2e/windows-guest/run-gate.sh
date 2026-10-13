#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Run the RDR-224 Phase 5 gate (tests/e2e/windows-phase5-gate.ps1) unattended in
# the Windows gate guest, from the Mac, and print the verdict line. Exits 0 only
# when the collected verdict line is "WINDOWS PHASE 5 GATE PASSED
# (executed=N declared=N)" with N equal; every other outcome exits non-zero.
#
#   tests/e2e/windows-guest/run-gate.sh --version X.Y.Z --engine A.B.C \
#       [--wheel PATH] [--out DIR] [--keep-guest]
#
# --version and --engine are the gate's ExpectedVersion and ExpectedEngine; there
# is no default for either. --wheel installs that candidate wheel instead of
# conexus==X.Y.Z from PyPI, and the run is then named a candidate wheel (not a
# published build) in GATERUN.txt and in the output. --out is where the logs and
# the collected run directory land (default: a new directory under $TMPDIR).
# --keep-guest leaves the guest running afterwards, for diagnosis; the host files
# are removed either way.
#
# What it does, in order (driver/ holds the host-side scripts, each staged under
# a per-run directory C:/build/guest/nxg-<UTC time>-<pid>/, so two runs cannot
# collide, and removed by exact name at the end):
#   0 hostcheck  read the host and guest, parse every staged script under
#                PowerShell 5.1, refuse if the guest is not Off or the
#                checkpoint is missing
#   1 restore    restore ONLY to the named checkpoint (none is taken), add the
#                Default Switch adapter when the restored configuration has none,
#                start, wait for the network, copy the gate script in, parse it
#                again on the guest
#   2 install    uv, conexus, Claude Code, Git, the plugin
#   3 setup      the gate's setup phase, a one-shot Interactive scheduled task
#   4 gaterun    write GATERUN.txt
#   5 launch     launch.sh: a signed-in Claude Code session that follows it
#   6 verdict    bounded wait for verdict.txt, read-only collection of the run
#   end          shut the guest down, remove the staged host files
#
# Credentials: the guest password comes from the macOS keychain and travels ONLY
# as `security find-generic-password ... -w | ssh "$HOST" powershell -File
# <script>`, read by the host script from stdin line 1. It is never in a shell
# variable, an argument, a file, a task definition or a log: no `set -x` and no
# transcript in anything that reads it, and the host scripts neither echo nor
# store it. The automation token stays inside launch.sh (RDR-219). The
# keychain item is the one launch.sh uses.
#
# Environment overrides (the names launch.sh reads, plus the checkpoint):
#   NX_GUEST_HOST, NX_GUEST_VM, NX_GUEST_USER, NX_GUEST_KEYCHAIN_SERVICE,
#   NX_GUEST_CHECKPOINT.
#
# Not run on Windows from CI: the PowerShell is carried over from the scripts
# that passed on 2026-10-12 (T2 nexus_rdr/224-gate-run-7.78.0-2026-10-12);
# tests/test_windows_gate_driver.py pins its shape.
# SC2029: the remote command lines are built on this side on purpose (the values
# are validated plain tokens below). SC2329: run_step calls its command through
# "$@", and on_exit, launch_guest run from a trap and run_step.
# shellcheck disable=SC2029,SC2329
set -uo pipefail

HOST="${NX_GUEST_HOST:-qwentescence}"
VM="${NX_GUEST_VM:-nx-clean-win11}"
GUSER="${NX_GUEST_USER:-nxguest}"
KEYCHAIN_SERVICE="${NX_GUEST_KEYCHAIN_SERVICE:-nexus-win-guest}"
CHECKPOINT="${NX_GUEST_CHECKPOINT:-clean-baseline-2026-10-06}"
export NX_GUEST_HOST="$HOST" NX_GUEST_VM="$VM" NX_GUEST_USER="$GUSER" NX_GUEST_KEYCHAIN_SERVICE="$KEYCHAIN_SERVICE"

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
gate="$repo/tests/e2e/windows-phase5-gate.ps1"
driver="$here/driver"

usage() {
    echo "usage: run-gate.sh --version X.Y.Z --engine A.B.C [--wheel PATH] [--out DIR] [--keep-guest]" >&2
}
die() {
    local code="$1"; shift
    echo "run-gate.sh: $*" >&2
    exit "$code"
}

version=''
engine=''
wheel=''
out=''
keep_guest=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) [[ $# -ge 2 ]] || { usage; exit 2; }; version="$2"; shift 2 ;;
        --engine) [[ $# -ge 2 ]] || { usage; exit 2; }; engine="$2"; shift 2 ;;
        --wheel) [[ $# -ge 2 ]] || { usage; exit 2; }; wheel="$2"; shift 2 ;;
        --out) [[ $# -ge 2 ]] || { usage; exit 2; }; out="$2"; shift 2 ;;
        --keep-guest) keep_guest=1; shift ;;
        *) usage; exit 2 ;;
    esac
done
[[ -n "$version" && -n "$engine" ]] || { usage; die 2 "--version and --engine are required"; }
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 2 "--version must be X.Y.Z"
[[ "$engine" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 2 "--engine must be X.Y.Z"
# These values reach a remote command line; keep them to plain tokens.
for pair in "NX_GUEST_HOST:$HOST" "NX_GUEST_VM:$VM" "NX_GUEST_USER:$GUSER" "NX_GUEST_KEYCHAIN_SERVICE:$KEYCHAIN_SERVICE" "NX_GUEST_CHECKPOINT:$CHECKPOINT"; do
    [[ "${pair#*:}" =~ ^[A-Za-z0-9._-]+$ ]] || die 2 "${pair%%:*} must be plain letters, digits, dot, dash or underscore"
done
[[ -f "$gate" ]] || die 2 "no gate script at $gate"

wheel_name=''
wheel_sha=''
if [[ -n "$wheel" ]]; then
    [[ -f "$wheel" ]] || die 2 "no such wheel: $wheel"
    wheel_name="$(basename "$wheel")"
    [[ "$wheel_name" =~ ^[A-Za-z0-9._+-]+\.whl$ ]] || die 2 "the wheel's file name has a character outside A-Za-z0-9._+-"
    [[ "$wheel_name" == "conexus-${version}-"* ]] || die 2 "the wheel is $wheel_name, not a conexus-${version}- wheel"
    wheel_sha="$(shasum -a 256 "$wheel" | awk '{print $1}')"
fi
gate_sha="$(shasum -a 256 "$gate" | awk '{print $1}')"

RUN="nxg-$(date -u +%Y%m%dT%H%M%SZ)-$$"
HOST_WIN_DIR="C:\\build\\guest\\$RUN"
HOST_DIR="C:/build/guest/$RUN"
out="${out:-${TMPDIR:-/tmp}/nxgate-run-$RUN}"
mkdir -p "$out" || die 2 "cannot create $out"
[[ -z "$(ls -A "$out")" ]] || die 2 "$out is not empty"

say() { echo "run-gate.sh: $(date -u +%H:%M:%S) $*"; }
quiet() { grep -v -i -E 'post-quantum|store now, decrypt later|pq\.html' || true; }

SSH_OPTS=(-o ServerAliveInterval=30 -o ServerAliveCountMax=6)

# A host script that needs no guest credential.
host_ps() {
    local script="$1"; shift
    ssh "${SSH_OPTS[@]}" "$HOST" "powershell -NoProfile -ExecutionPolicy Bypass -File $HOST_WIN_DIR\\$script $*" </dev/null
}
# A host script that opens the guest: the password goes on stdin, line 1, and
# nowhere else.
host_ps_pw() {
    local script="$1"; shift
    security find-generic-password -a "$GUSER" -s "$KEYCHAIN_SERVICE" -w \
        | ssh "${SSH_OPTS[@]}" "$HOST" "powershell -NoProfile -ExecutionPolicy Bypass -File $HOST_WIN_DIR\\$script $*"
}
launch_guest() {
    "$here/launch.sh" --stage "$gate" --permission-mode auto \
        --prompt "follow the instructions in C:\\Users\\$GUSER\\nx-gate\\GATERUN.txt" </dev/null
}

host_dir_made=0
guest_touched=0
collected=0
finished=0

# run_step <nn> <label> <require-ok-marker 0|1> <command...>: runs the command,
# logs its output to $out/<nn>-<label>.txt, and stops the run on a non-zero
# exit or, when asked, on a missing "NXG-RESULT: ok" line.
run_step() {
    local n="$1" label="$2" marker="$3" log rc
    shift 3
    log="$out/$n-$label.txt"
    say "step $n $label"
    echo "$label start $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee "$log"
    "$@" 2>&1 | quiet | tee -a "$log"
    rc=${PIPESTATUS[0]}
    echo "$label end $(date -u +%Y-%m-%dT%H:%M:%SZ) exit=$rc" | tee -a "$log"
    [[ $rc -eq 0 ]] || die 1 "step $label failed (exit $rc); log: $log"
    if [[ "$marker" -eq 1 ]] && ! grep -q '^NXG-RESULT: ok' "$log"; then
        die 1 "step $label did not report NXG-RESULT: ok; log: $log"
    fi
}

# Copy the guest's run directory, collected on the host, back to $out/run.
fetch_collected() {
    [[ ! -e "$out/run" ]] || return 0
    scp -q -r "$HOST:$HOST_DIR/out" "$out/" 2>&1 | quiet
    if [[ -d "$out/out" ]]; then mv "$out/out" "$out/run"; collected=1; fi
}

# Always runs: a best-effort collection when the guest was touched and nothing
# was collected yet, then the shutdown and the removal of the staged host files.
finish() {
    [[ $finished -eq 0 ]] || return 0
    finished=1
    [[ $host_dir_made -eq 1 ]] || return 0
    local keep=''
    if [[ $guest_touched -eq 1 && $collected -eq 0 ]]; then
        say "collecting what the guest holds"
        { host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Collect yes 2>&1 | quiet; } >"$out/90-collect-on-exit.txt" 2>&1 || true
        fetch_collected || true
    fi
    # Never shut down a guest this run did not restore, and not when asked to keep it.
    if [[ $keep_guest -eq 1 || $guest_touched -eq 0 ]]; then keep='-KeepGuest'; fi
    say "cleaning up the host"
    local wheel_arg=''
    if [[ -n "$wheel_name" ]]; then wheel_arg="-Wheel $wheel_name"; fi
    # shellcheck disable=SC2086
    host_ps end.ps1 -VMName "$VM" -RunDir "$HOST_WIN_DIR" $wheel_arg $keep 2>&1 | quiet | tee "$out/99-end.txt" || true
}
on_exit() {
    local rc=$?
    trap - EXIT
    finish
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -n "$wheel_name" ]]; then
    say "candidate wheel $wheel_name sha256 $wheel_sha (not a published build)"
else
    say "conexus==$version from PyPI"
fi
say "engine $engine, gate script sha256 $gate_sha, run $RUN, logs in $out"

# --- stage ------------------------------------------------------------------
# mkdir fails when the directory exists, so a collision stops here.
ssh "${SSH_OPTS[@]}" "$HOST" "cmd /c \"mkdir $HOST_WIN_DIR\\stage\"" </dev/null 2>&1 | quiet
[[ ${PIPESTATUS[0]} -eq 0 ]] || die 1 "cannot create $HOST_DIR on $HOST"
host_dir_made=1
scp -q "$driver"/*.ps1 "$HOST:$HOST_DIR/" 2>&1 | quiet
[[ ${PIPESTATUS[0]} -eq 0 ]] || die 1 "cannot copy the driver scripts to $HOST:$HOST_DIR"
if [[ -n "$wheel_name" ]]; then
    scp -q "$gate" "$wheel" "$HOST:$HOST_DIR/stage/" 2>&1 | quiet
else
    scp -q "$gate" "$HOST:$HOST_DIR/stage/" 2>&1 | quiet
fi
[[ ${PIPESTATUS[0]} -eq 0 ]] || die 1 "cannot copy the gate script to $HOST:$HOST_DIR/stage"

# --- run --------------------------------------------------------------------
run_step 00 hostcheck 1 host_ps hostcheck.ps1 -VMName "$VM" -Checkpoint "$CHECKPOINT" -RunDir "$HOST_WIN_DIR"

guest_touched=1
if [[ -n "$wheel_name" ]]; then
    run_step 01 restore 1 host_ps_pw restore.ps1 -VMName "$VM" -GuestUser "$GUSER" -Checkpoint "$CHECKPOINT" -RunDir "$HOST_WIN_DIR" -Wheel "$wheel_name"
    grep -qF "wheel sha256=$wheel_sha" "$out/01-restore.txt" || die 1 "the wheel in the guest does not have sha256 $wheel_sha"
else
    run_step 01 restore 1 host_ps_pw restore.ps1 -VMName "$VM" -GuestUser "$GUSER" -Checkpoint "$CHECKPOINT" -RunDir "$HOST_WIN_DIR"
fi
grep -qF "gate sha256=$gate_sha" "$out/01-restore.txt" || die 1 "the gate script in the guest does not have sha256 $gate_sha"

if [[ -n "$wheel_name" ]]; then
    run_step 02 install 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner inst.ps1 -Arg1 "$version" -Arg2 "$wheel_name"
else
    run_step 02 install 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner inst.ps1 -Arg1 "$version"
fi
run_step 03 setup 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner setup-run.ps1 -Arg1 "$version" -Arg2 "$engine"
if [[ -n "$wheel_name" ]]; then
    run_step 04 gaterun 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner gaterun.ps1 -Arg1 "$version" -Arg2 "$engine" -Arg3 candidate
else
    run_step 04 gaterun 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner gaterun.ps1 -Arg1 "$version" -Arg2 "$engine"
fi
run_step 05 launch 0 launch_guest
run_step 06 verdict 0 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner waitv.ps1 -Collect yes

fetch_collected
[[ $collected -eq 1 ]] || die 1 "the guest's run directory was not collected"

# --- verdict ----------------------------------------------------------------
verdict_file="$out/run/verdict.txt"
[[ -f "$verdict_file" ]] || die 1 "no verdict.txt was collected: the run has no verdict, which is a FAILED run"
verdict="$(grep -v '^[[:space:]]*$' "$verdict_file" | tail -n 1 | tr -d '\r' | LC_ALL=C sed $'s/^\xef\xbb\xbf//')"
passed_re='^WINDOWS PHASE 5 GATE PASSED \(executed=([0-9]+) declared=([0-9]+)\)$'
rc=1
if [[ "$verdict" =~ $passed_re ]] && [[ "${BASH_REMATCH[1]}" == "${BASH_REMATCH[2]}" ]] && [[ "${BASH_REMATCH[1]}" -gt 0 ]]; then
    rc=0
fi
finish
echo "$verdict"
if [[ -n "$wheel_name" ]]; then
    echo "run-gate.sh: candidate wheel $wheel_name (sha256 $wheel_sha), not a published build; gate script sha256 $gate_sha; logs in $out"
else
    echo "run-gate.sh: conexus==$version from PyPI; gate script sha256 $gate_sha; logs in $out"
fi
exit "$rc"
