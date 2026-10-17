#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build (or rebuild) the clean macOS guest image on hellmini, from the laptop
# (nexus-ppuwc). One-time host work; launch.sh clones the result per run.
#
#   tests/e2e/macos-guest/build-image.sh [--source OCI-REF]
#
# Source default: ghcr.io/cirruslabs/macos-golden-gate-base:latest (macOS 27,
# Cirrus Labs; account admin with password admin, automatic login, Remote Login
# on, passwordless sudo, FileVault off, Homebrew and the Tart guest agent).
#
#   1 ensure the guest key on hellmini ($BASE/ssh/id_ed25519; it never leaves
#     the host) and pull the source image when tart does not have it
#   2 clone it to nexus-mac-build, 4 CPU / 6 GB, boot it headless
#   3 through the guest agent (tart exec) authorize the key for admin
#   4 over ssh run provision.sh: uv and Claude Code into ~/.local/bin, then check
#     FileVault off, automatic login, passwordless sudo, no nx
#   5 power the guest off and rename it to nexus-mac-clean, replacing an old one
#
# Refuses (exit 75) while a suite holds the hellmini lease or a CI job runs.
# SC2029: remote command lines are built here on purpose (plain tokens).
# shellcheck disable=SC2029
set -uo pipefail

HOST="${NX_MAC_GUEST_HOST:-hellmini}"
BASE="${NX_MAC_GUEST_DIR:-/Volumes/Bulk/tart/nexus}"
SRC=ghcr.io/cirruslabs/macos-golden-gate-base:latest
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) [[ $# -ge 2 ]] || { echo "build-image.sh: --source needs a ref" >&2; exit 2; }; SRC="$2"; shift 2 ;;
        *) echo "usage: build-image.sh [--source OCI-REF]" >&2; exit 2 ;;
    esac
done
[[ "$SRC" =~ ^[A-Za-z0-9._/:@-]+$ ]] || { echo "build-image.sh: --source must be a plain OCI reference" >&2; exit 2; }
here="$(cd "$(dirname "$0")" && pwd)"
SSH_OPTS=(-o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=6)
say() { echo "build-image.sh: $(date -u +%H:%M:%S) $*"; }
r() { ssh "${SSH_OPTS[@]}" "$HOST" "$*" </dev/null; }
die() { say "FAILED: $*"; exit 1; }

r "mkdir -p $BASE/ssh $BASE/logs && chmod 700 $BASE/ssh" || die "cannot create $BASE on $HOST"
scp "${SSH_OPTS[@]}" -q "$here/host.sh" "$here/provision.sh" "$HOST:$BASE/" </dev/null || die "cannot copy host.sh"
r "chmod +x $BASE/host.sh" || die "chmod"
r "$BASE/host.sh tart --version" >/dev/null || die "tart is not installed on $HOST, or its account has no GUI session (README: host setup)"
r "$BASE/host.sh guard"; rc=$?
[[ $rc -eq 75 ]] && { say "hellmini is busy; not building"; exit 75; }
[[ $rc -eq 0 ]] || die "guard exit $rc"
r "test -f $BASE/ssh/id_ed25519 || ssh-keygen -q -t ed25519 -N '' -C nexus-mac-guest -f $BASE/ssh/id_ed25519" || die "ssh-keygen"
if ! r "$BASE/host.sh tart get $SRC >/dev/null 2>&1"; then
    say "pulling $SRC (tens of GB)"
    r "df -h /Volumes/Bulk | tail -1; $BASE/host.sh tart pull $SRC >$BASE/logs/pull.log 2>&1" || die "tart pull $SRC (log $BASE/logs/pull.log)"
fi
cleanup() { r "$BASE/host.sh destroy nexus-mac-build" >/dev/null 2>&1 || true; }
r "$BASE/host.sh destroy nexus-mac-build" >/dev/null 2>&1 || true
r "$BASE/host.sh build-start $SRC" || die "build-start"
trap cleanup EXIT

say "waiting for the guest agent"
ok=0
for _ in $(seq 1 100); do
    if r "$BASE/host.sh tart exec nexus-mac-build /usr/bin/true" >/dev/null 2>&1; then ok=1; break; fi
    sleep 6
done
[[ $ok -eq 1 ]] || die "the guest agent never answered (tart exec)"
r "$BASE/host.sh tart exec -i nexus-mac-build /bin/bash -c 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >>~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys' <$BASE/ssh/id_ed25519.pub" \
    || die "cannot authorize the guest key"
r "$BASE/host.sh waitssh nexus-mac-build 300" >/dev/null || die "no ssh into the build VM"
r "$BASE/host.sh gscp nexus-mac-build $BASE/provision.sh /Users/admin/provision.sh" || die "cannot stage provision.sh"
say "provision"
r "$BASE/host.sh gssh nexus-mac-build 'bash ~/provision.sh && rm -f ~/provision.sh'" || die "provision.sh failed"
say "power off, then rename to nexus-mac-clean"
r "$BASE/host.sh gssh nexus-mac-build 'sudo -n shutdown -h now'" >/dev/null 2>&1 || true
r "$BASE/host.sh build-finish" || die "build-finish"
trap - EXIT
r "rm -f $BASE/provision.sh $BASE/logs/nexus-mac-build.run.log; $BASE/host.sh tart list; du -sh /Volumes/Bulk/tart/home"
say "done: nexus-mac-clean from $SRC"
