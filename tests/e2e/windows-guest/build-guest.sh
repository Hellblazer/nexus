#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build a Windows 11 gate guest on the Hyper-V host from Microsoft's consumer
# ISO, from the Mac: stage build-guest.ps1 under a per-run directory on the
# host, run it with the guest password on stdin, remove the directory. The guest
# it makes is what run-gate.sh restores (NX_GUEST_VM, NX_GUEST_CHECKPOINT).
#
#   tests/e2e/windows-guest/build-guest.sh --vm NAME --edition 'Windows 11 Pro' \
#       --computer NXPRO --checkpoint clean-baseline-pro-YYYY-MM-DD
#
# The ISO must already be on the host at C:\build\guest\win11-consumer-26300.iso
# with the sha256 build-guest.ps1 names (Microsoft's published hash for that
# download). --edition is an image name from the ISO: 'Windows 11 Home',
# 'Windows 11 Pro', 'Windows 11 Education' and their N variants. Takes about 25
# minutes. Refuses when the VM or its directory exists, or the release runner is
# busy; touches no other VM.
#
# Credentials: the password comes from the macOS keychain and travels ONLY as
# `security find-generic-password ... -w | ssh "$HOST" powershell -File
# <script>`, as in run-gate.sh. The guest keeps it only as its automatic
# sign-in secret.
# SC2029: the remote command line is built here on purpose (validated tokens).
# SC2329: cleanup runs from the EXIT trap.
# shellcheck disable=SC2029,SC2329
set -uo pipefail

HOST="${NX_GUEST_HOST:-qwentescence}"
GUSER="${NX_GUEST_USER:-nxguest}"
KEYCHAIN_SERVICE="${NX_GUEST_KEYCHAIN_SERVICE:-nexus-win-guest}"
here="$(cd "$(dirname "$0")" && pwd)"

die() { local code="$1"; shift; echo "build-guest.sh: $*" >&2; exit "$code"; }
usage() { echo "usage: build-guest.sh --vm NAME --edition 'Windows 11 <Edition>' --computer NAME --checkpoint NAME" >&2; }

vm=''; edition=''; computer=''; checkpoint=''
while [[ $# -gt 0 ]]; do
    case "$1" in
        --vm) [[ $# -ge 2 ]] || { usage; exit 2; }; vm="$2"; shift 2 ;;
        --edition) [[ $# -ge 2 ]] || { usage; exit 2; }; edition="$2"; shift 2 ;;
        --computer) [[ $# -ge 2 ]] || { usage; exit 2; }; computer="$2"; shift 2 ;;
        --checkpoint) [[ $# -ge 2 ]] || { usage; exit 2; }; checkpoint="$2"; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -n "$vm" && -n "$edition" && -n "$computer" && -n "$checkpoint" ]] || { usage; exit 2; }
for v in "$vm" "$checkpoint"; do [[ "$v" =~ ^[A-Za-z0-9._-]+$ ]] || die 2 "$v has a character outside A-Za-z0-9._-"; done
[[ "$computer" =~ ^[A-Za-z0-9-]{1,15}$ ]] || die 2 "the computer name must be 1 to 15 of A-Za-z0-9-"
[[ "$edition" =~ ^Windows\ 11\ [A-Za-z\ ]+$ ]] || die 2 "the edition must look like 'Windows 11 Pro'"
shopt -s nocasematch
[[ "$vm$computer$checkpoint" =~ qwen|llama ]] && die 2 "names on this host may not contain qwen or llama"
shopt -u nocasematch

SSH_OPTS=(-o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=6)
RUN="nxb-$(date -u +%Y%m%dT%H%M%SZ)-$$"
HOST_WIN_DIR="C:\\build\\guest\\$RUN"
HOST_DIR="C:/build/guest/$RUN"

cleanup() { ssh "${SSH_OPTS[@]}" "$HOST" "cmd /c \"rmdir /s /q $HOST_WIN_DIR\"" </dev/null >/dev/null 2>&1; }
trap cleanup EXIT

ssh "${SSH_OPTS[@]}" "$HOST" "cmd /c \"mkdir $HOST_WIN_DIR\"" </dev/null >/dev/null || die 1 "cannot create $HOST_WIN_DIR on $HOST"
scp "${SSH_OPTS[@]}" -q "$here/build-guest.ps1" "$HOST:$HOST_DIR/" || die 1 "cannot stage build-guest.ps1"
log="$(mktemp "${TMPDIR:-/tmp}/build-guest.XXXXXX")"
security find-generic-password -a "$GUSER" -s "$KEYCHAIN_SERVICE" -w \
    | ssh "${SSH_OPTS[@]}" "$HOST" "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $HOST_WIN_DIR\\build-guest.ps1 -VMName $vm -Edition '$edition' -ComputerName $computer -GuestUser $GUSER -Checkpoint $checkpoint" \
    | tee "$log"
# Only the result line says what happened: a non-zero exit of the host script
# reaches this side as 1 whatever it was.
if grep -q '^BUILD-GUEST-RESULT: ok' "$log"; then
    rm -f "$log"
    echo "build-guest.sh: built $vm with checkpoint $checkpoint"
    exit 0
fi
echo "build-guest.sh: FAILED; the log is $log" >&2
exit 1
