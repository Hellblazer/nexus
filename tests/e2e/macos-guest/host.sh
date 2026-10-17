#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host side of the macOS test guest (nexus-ppuwc). Runs ON hellmini; launch.sh
# and build-image.sh stage it into a per-run directory and call it over ssh:
#
#   host.sh guard                 exit 75 when a suite or engine build holds the
#                                 lease, or a GitHub runner job is running
#   host.sh preflight             exit 3 when tart, the clean image or the guest
#                                 key is missing
#   host.sh start VM [SRC]        clone SRC (default the clean image) to VM, boot it
#                                 headless in the background
#   host.sh waitssh VM SECS       wait until ssh into VM answers; prints its address
#   host.sh gssh VM CMD...        ssh into VM as admin and run CMD; stdin passes
#                                 through (claude_credentials.py run --remote uses
#                                 this as its remote shell)
#   host.sh gscp VM SRC... DEST   scp files into VM (DEST is a guest path)
#   host.sh gfetch VM SRC DEST    scp -r a guest path out to a host path
#   host.sh reboot VM SECS        reboot the guest from inside and wait for ssh to
#                                 come back; refuses a VM whose tart run has gone
#   host.sh destroy VM            stop VM and delete it (only nx-mac-run-* names
#                                 and nexus-mac-build)
#   host.sh build-start SRC       (build-image.sh) clone SRC to nexus-mac-build,
#                                 size it, boot it
#   host.sh tart ARGS...          any tart command, as the tart account
#   host.sh build-finish          (build-image.sh) after the build VM powers off,
#                                 replace the clean image with it
#
# Every tart call runs as $RUN_AS inside its GUI login session (see t below).
# Never reads or prints a credential. The guest key lives in $KEYDIR on the
# Bulk volume and only authenticates to throwaway guests on tart's NAT network.
set -uo pipefail

export TART_HOME="${TART_HOME:-/Volumes/Bulk/tart/home}"
BASE="${NX_MAC_GUEST_DIR:-/Volumes/Bulk/tart/nexus}"
KEYDIR="$BASE/ssh"
CLEAN="${NX_MAC_GUEST_IMAGE:-nexus-mac-clean}"
LEASE_REPO="${NX_MAC_GUEST_LEASE_REPO:-/Volumes/Bulk/src/nexus}"
RUNNER_USERS="${NX_MAC_GUEST_RUNNER_USERS:-ghrunner ghci}"
TART=/opt/homebrew/bin/tart
# 6 GB. Measured in the guest on 2026-10-17 (memory_pressure): 81% free before
# setup, 54% free with the local stack up and a Claude Code session running
# (about 2.8 GB in use), so 4 GB would leave little headroom and 8 (Cirrus's
# default) is a third of hellmini. hellmini has 24 GB and a full suite needs
# ~23, which is why guard refuses to start the guest while a suite holds the
# lease.
CPUS="${NX_MAC_GUEST_CPUS:-4}"
MEMORY_MB="${NX_MAC_GUEST_MEMORY_MB:-6144}"
RUN_AS="${NX_MAC_GUEST_RUN_AS:-nxmvv}"
[[ "$RUN_AS" =~ ^[a-z0-9_]+$ ]] || { echo "host.sh: NX_MAC_GUEST_RUN_AS must be a plain account name" >&2; exit 2; }
RUN_UID="$(id -u "$RUN_AS" 2>/dev/null || true)"
SSH_OPTS=(-i "$KEYDIR/id_ed25519" -o IdentitiesOnly=yes -o BatchMode=yes
          -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
          -o ConnectTimeout=8 -o ServerAliveInterval=15 -o ServerAliveCountMax=8)

die() { local c="$1"; shift; echo "host.sh: $*" >&2; exit "$c"; }

valid_vm() {
    [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] || die 2 "bad VM name: $1"
}

# Virtualization.framework boots a macOS guest only from a process in a GUI
# login session (outside one, tart run fails with VZErrorDomain Code=-9,
# "Failed to get current host key"; measured 2026-10-17, also as root).
# hellmini has FileVault, so no account logs in by itself; tart runs as the
# throwaway account that stays signed in there (nxmvv), inside its session.
# TART_HOME belongs to that account.
t() { sudo -n launchctl asuser "$RUN_UID" sudo -n -u "$RUN_AS" env TART_HOME="$TART_HOME" "$TART" "$@"; }

guest_ip() { t ip "$1" --wait 5 2>/dev/null; }

# The suite and engine-build leases of the clone's git common dir
# (scripts/lib/build-lease.sh: nexus-build-lease/<name>/pid), and a
# Runner.Worker of either runner user, are what claim hellmini's RAM.
cmd_guard() {
    local common busy=0 name pid u
    # NX_MAC_GUEST_LEASE_ROOT stands in for the git common dir (tests of this guard).
    common="${NX_MAC_GUEST_LEASE_ROOT:-}"
    if [[ -z "$common" ]]; then
        common="$(git -C "$LEASE_REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
            || die 3 "cannot find the git common dir of $LEASE_REPO (lease check impossible)"
    fi
    for name in suite service; do
        pid="$(cat "$common/nexus-build-lease/$name/pid" 2>/dev/null || true)"
        if [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1; then
            echo "host.sh: busy: pid $pid holds nexus-build-lease/$name in $common" >&2; busy=1
        fi
    done
    for u in $RUNNER_USERS; do
        if pgrep -u "$u" -f Runner.Worker >/dev/null 2>&1; then
            echo "host.sh: busy: runner user $u has a Runner.Worker (a CI job is running)" >&2; busy=1
        fi
    done
    [[ $busy -eq 0 ]] || exit 75
    echo "guard: free (no live lease holder, no Runner.Worker)"
}

cmd_preflight() {
    [[ -x "$TART" ]] || die 3 "tart is not installed at $TART (README: host setup)"
    [[ -f "$KEYDIR/id_ed25519" ]] || die 3 "no guest key at $KEYDIR/id_ed25519 (run build-image.sh)"
    t get "$CLEAN" >/dev/null 2>&1 || die 3 "no clean image $CLEAN in $TART_HOME (run build-image.sh)"
    echo "preflight: tart $(t --version) as $RUN_AS (GUI session gui/$RUN_UID), image $CLEAN, key present"
}

cmd_start() {
    local vm="$1" src="${2:-$CLEAN}" log
    valid_vm "$vm"
    t get "$vm" >/dev/null 2>&1 && die 1 "VM $vm already exists"
    t clone "$src" "$vm" || die 1 "tart clone $src $vm failed"
    log="$BASE/logs/$vm.run.log"
    mkdir -p "$BASE/logs"
    nohup sudo -n launchctl asuser "$RUN_UID" sudo -n -u "$RUN_AS" env TART_HOME="$TART_HOME" "$TART" run --no-graphics "$vm" >"$log" 2>&1 </dev/null &
    echo "started $vm from $src (tart run pid $!, log $log)"
}

run_alive() { pgrep -f "tart run --no-graphics $1\$" >/dev/null 2>&1; }

cmd_waitssh() {
    local vm="$1" secs="$2" end ip
    valid_vm "$vm"
    end=$(( $(date +%s) + secs ))
    while [[ $(date +%s) -lt $end ]]; do
        run_alive "$vm" || die 1 "tart run for $vm is not running"
        ip="$(guest_ip "$vm")"
        if [[ -n "$ip" ]] && ssh "${SSH_OPTS[@]}" "admin@$ip" true </dev/null 2>/dev/null; then
            echo "$ip"; return 0
        fi
        sleep 3
    done
    die 1 "no ssh into $vm within $secs s"
}

cmd_gssh() {
    local vm="$1" ip; shift
    valid_vm "$vm"
    ip="$(guest_ip "$vm")"; [[ -n "$ip" ]] || die 1 "no address for $vm"
    exec ssh "${SSH_OPTS[@]}" "admin@$ip" "$@"
}

cmd_gscp() {
    local vm="$1" ip dest; shift
    valid_vm "$vm"
    dest="${*: -1}"; set -- "${@:1:$#-1}"
    ip="$(guest_ip "$vm")"; [[ -n "$ip" ]] || die 1 "no address for $vm"
    scp -q -r "${SSH_OPTS[@]}" "$@" "admin@$ip:$dest"
}

cmd_gfetch() {
    local vm="$1" src="$2" dest="$3" ip
    valid_vm "$vm"
    ip="$(guest_ip "$vm")"; [[ -n "$ip" ]] || die 1 "no address for $vm"
    scp -q -r "${SSH_OPTS[@]}" "admin@$ip:$src" "$dest"
}

# A guest-initiated reboot. The boot time must change and the same tart run
# must still own the VM: a VM that stopped instead of rebooting is a failure,
# not something to paper over with a cold start.
cmd_reboot() {
    local vm="$1" secs="$2" ip before after end down=0
    valid_vm "$vm"
    ip="$(guest_ip "$vm")"; [[ -n "$ip" ]] || die 1 "no address for $vm"
    before="$(ssh "${SSH_OPTS[@]}" "admin@$ip" 'sysctl -n kern.boottime' </dev/null)" || die 1 "cannot read boot time"
    echo "reboot: boot before: $before"
    ssh "${SSH_OPTS[@]}" "admin@$ip" 'sudo -n shutdown -r now' </dev/null >/dev/null 2>&1 || true
    end=$(( $(date +%s) + secs ))
    while [[ $(date +%s) -lt $end ]]; do
        run_alive "$vm" || die 1 "tart run for $vm exited during the reboot (the VM stopped instead of rebooting)"
        ip="$(guest_ip "$vm")"
        if [[ -n "$ip" ]] && after="$(ssh "${SSH_OPTS[@]}" "admin@$ip" 'sysctl -n kern.boottime' </dev/null 2>/dev/null)"; then
            if [[ "$after" != "$before" ]]; then
                echo "reboot: boot after: $after (ssh back at $ip, down seen: $down)"; return 0
            fi
        else
            down=1
        fi
        sleep 3
    done
    die 1 "the guest did not come back with a new boot time within $secs s"
}

# build-image.sh only. The build VM gets the guest size every clone inherits.
cmd_build_start() {
    local src="$1" log
    t get nexus-mac-build >/dev/null 2>&1 && die 1 "nexus-mac-build already exists (host.sh destroy nexus-mac-build)"
    t clone "$src" nexus-mac-build || die 1 "tart clone $src nexus-mac-build failed"
    t set nexus-mac-build --cpu "$CPUS" --memory "$MEMORY_MB" || die 1 "tart set failed"
    log="$BASE/logs/nexus-mac-build.run.log"; mkdir -p "$BASE/logs"
    nohup sudo -n launchctl asuser "$RUN_UID" sudo -n -u "$RUN_AS" env TART_HOME="$TART_HOME" "$TART" run --no-graphics nexus-mac-build >"$log" 2>&1 </dev/null &
    echo "started nexus-mac-build from $src: $CPUS CPU, $MEMORY_MB MB (tart run pid $!)"
}

# Wait for the build VM to power off, then make it the clean image.
cmd_build_finish() {
    for _ in $(seq 1 60); do run_alive nexus-mac-build || break; sleep 3; done
    run_alive nexus-mac-build && die 1 "nexus-mac-build did not power off"
    if t get "$CLEAN" >/dev/null 2>&1; then t delete "$CLEAN" || die 1 "cannot delete the old $CLEAN"; fi
    t rename nexus-mac-build "$CLEAN" || die 1 "tart rename failed"
    t get "$CLEAN"
}

cmd_destroy() {
    local vm="$1"
    valid_vm "$vm"
    [[ "$vm" == nx-mac-run-* || "$vm" == nexus-mac-build ]] || die 2 "destroy refuses $vm (only nx-mac-run-* and nexus-mac-build)"
    if t get "$vm" >/dev/null 2>&1; then
        if run_alive "$vm"; then
            t stop "$vm" --timeout 60 >/dev/null 2>&1 || true
            for _ in $(seq 1 30); do run_alive "$vm" || break; sleep 2; done
            run_alive "$vm" && die 1 "$vm is still running after tart stop"
        fi
        t delete "$vm" || die 1 "tart delete $vm failed"
    fi
    t get "$vm" >/dev/null 2>&1 && die 1 "$vm still exists"
    echo "destroyed $vm"
}

verb="${1:-}"; shift || true
case "$verb" in
    guard) cmd_guard ;;
    preflight) cmd_preflight ;;
    start) [[ $# -ge 1 ]] || die 2 "start VM [SRC]"; cmd_start "$@" ;;
    waitssh) [[ $# -eq 2 ]] || die 2 "waitssh VM SECS"; cmd_waitssh "$@" ;;
    gssh) [[ $# -ge 2 ]] || die 2 "gssh VM CMD..."; cmd_gssh "$@" ;;
    gscp) [[ $# -ge 3 ]] || die 2 "gscp VM SRC... DEST"; cmd_gscp "$@" ;;
    gfetch) [[ $# -eq 3 ]] || die 2 "gfetch VM SRC DEST"; cmd_gfetch "$@" ;;
    reboot) [[ $# -eq 2 ]] || die 2 "reboot VM SECS"; cmd_reboot "$@" ;;
    destroy) [[ $# -eq 1 ]] || die 2 "destroy VM"; cmd_destroy "$@" ;;
    build-start) [[ $# -eq 1 ]] || die 2 "build-start SRC"; cmd_build_start "$@" ;;
    build-finish) cmd_build_finish ;;
    tart) t "$@" ;;
    *) die 2 "unknown verb '$verb' (guard preflight start waitssh gssh gscp gfetch reboot destroy)" ;;
esac
