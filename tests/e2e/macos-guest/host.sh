#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Host side of the macOS test guest (nexus-ppuwc). Runs ON hellmini; launch.sh
# and build-image.sh stage it into a per-run directory and call it over ssh:
#
#   host.sh guard                 exit 75 when a suite or engine build holds the
#                                 lease (an unreadable lease counts as held), a
#                                 GitHub runner job runs, or a tart VM or tart
#                                 run process of ours exists
#   host.sh preflight             exit 3 when tart, the clean image, the guest key
#                                 or its pinned host key is missing, the tart
#                                 account is not signed in, or the files this
#                                 script trusts are writable by someone else
#   host.sh hold VM [keep]        acquire the hellmini `suite` lease and keep it
#                                 until stdin closes (or the lifetime cap); the
#                                 EXIT trap destroys VM, then releases the lease.
#                                 With `keep`, EOF on stdin leaves VM running and
#                                 the lease held until VM is gone or the cap
#   host.sh start VM [SRC]        clone SRC (default the clean image) to VM, boot it
#                                 headless in the background, start the deadman
#   host.sh waitssh VM SECS       wait until ssh into VM answers; prints its address
#   host.sh gssh VM CMD...        ssh into VM as admin and run CMD; stdin passes
#                                 through (claude_credentials.py run --remote uses
#                                 this as its remote shell)
#   host.sh gscp VM SRC... DEST   scp files into VM (DEST is a guest path)
#   host.sh gfetch VM SRC DEST    scp -r a guest path out to a host path
#   host.sh reboot VM SECS        reboot the guest from inside; success needs a new
#                                 kern.bootsessionuuid AND ssh seen down in between
#                                 AND the same tart run still owning the VM
#   host.sh destroy VM            stop VM and delete it, retried (only nx-mac-run-*
#                                 names and nexus-mac-build)
#   host.sh hostkey nexus-mac-build  (build-image.sh) pin the build guest's ed25519
#                                 host key, read through the Tart guest agent
#   host.sh build-start SRC       (build-image.sh) clone SRC to nexus-mac-build,
#                                 size it, boot it
#   host.sh tart ARGS...          any tart command, as the tart account
#   host.sh build-finish          (build-image.sh) after the build VM powers off,
#                                 replace the clean image with it
#
# Every tart call runs as $RUN_AS inside its GUI login session (see t below).
# Never reads or prints a credential. The guest key lives in $KEYDIR on the
# Bulk volume and only authenticates to throwaway guests on tart's NAT network.
#
# Overrides of paths, commands and timings (NX_MAC_GUEST_DIR, _TART, _LEASE_ROOT,
# _LEASE_REPO, _RUNNER_USERS, _RUN_UID, _TEST_ROOT, _LIFETIME_SECS, _STOP_WAIT)
# are read only when NX_MAC_GUEST_TEST=1, so an environment variable alone never
# redirects the guard, the lease or the files this script trusts.
set -uo pipefail

TESTING=0
[[ "${NX_MAC_GUEST_TEST:-}" == 1 ]] && TESTING=1
if [[ $TESTING -eq 1 ]]; then
    BASE="${NX_MAC_GUEST_DIR:-/Volumes/Bulk/tart/nexus}"
    TART="${NX_MAC_GUEST_TART:-/opt/homebrew/bin/tart}"
    LEASE_REPO="${NX_MAC_GUEST_LEASE_REPO:-/Volumes/Bulk/src/nexus}"
    LEASE_COMMON="${NX_MAC_GUEST_LEASE_ROOT:-}"
    RUNNER_USERS="${NX_MAC_GUEST_RUNNER_USERS:-ghrunner ghci}"
    LIFETIME_SECS="${NX_MAC_GUEST_LIFETIME_SECS:-5400}"
    STOP_WAIT="${NX_MAC_GUEST_STOP_WAIT:-60}"
    RETRY_SLEEP="${NX_MAC_GUEST_RETRY_SLEEP:-3}"
    TEST_ROOT="${NX_MAC_GUEST_TEST_ROOT:-}"
    RUN_UID_OVERRIDE="${NX_MAC_GUEST_RUN_UID:-}"
    export TART_HOME="${TART_HOME:-/Volumes/Bulk/tart/home}"
else
    BASE=/Volumes/Bulk/tart/nexus
    TART=/opt/homebrew/bin/tart
    LEASE_REPO=/Volumes/Bulk/src/nexus
    LEASE_COMMON=''
    # ghci was deregistered 2026-10-04 (infra/hellmini/README.md) but its account
    # still exists. A listed user that does not exist is skipped by name, never read
    # as "no job running"; the guard needs at least one user it could really check.
    RUNNER_USERS="ghrunner ghci"
    # The cap on a guest's life: after it, the deadman stops and deletes the VM.
    # A full run takes 3 to 12 minutes.
    LIFETIME_SECS=5400
    STOP_WAIT=60
    RETRY_SLEEP=3
    TEST_ROOT=''
    RUN_UID_OVERRIDE=''
    export TART_HOME=/Volumes/Bulk/tart/home
fi
KEYDIR="$BASE/ssh"
CLEAN="${NX_MAC_GUEST_IMAGE:-nexus-mac-clean}"
# 6 GB. Measured in the guest on 2026-10-17 (memory_pressure): 81% free before
# setup, 54% free with the local stack up and a Claude Code session running
# (about 2.8 GB in use), so 4 GB would leave little headroom and 8 (Cirrus's
# default) is a third of hellmini. hellmini has 24 GB and a full suite needs
# ~23, which is why a run holds the suite lease for its whole duration.
CPUS="${NX_MAC_GUEST_CPUS:-4}"
MEMORY_MB="${NX_MAC_GUEST_MEMORY_MB:-6144}"
RUN_AS="${NX_MAC_GUEST_RUN_AS:-nxmvv}"
[[ "$RUN_AS" =~ ^[a-z0-9_]+$ ]] || { echo "host.sh: NX_MAC_GUEST_RUN_AS must be a plain account name" >&2; exit 2; }
RUN_UID="${RUN_UID_OVERRIDE:-$(id -u "$RUN_AS" 2>/dev/null || true)}"
# The guest's host key is pinned in known_hosts at image build, so the guest hop
# is StrictHostKeyChecking=yes against that file and nothing else.
SSH_OPTS=(-i "$KEYDIR/id_ed25519" -o IdentitiesOnly=yes -o BatchMode=yes -o PasswordAuthentication=no
          -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KEYDIR/known_hosts" -o GlobalKnownHostsFile=/dev/null
          -o HostKeyAlgorithms=ssh-ed25519 -o LogLevel=ERROR
          -o ConnectTimeout=8 -o ServerAliveInterval=15 -o ServerAliveCountMax=8)
PKILL=/usr/bin/pkill

die() { local c="$1"; shift; echo "host.sh: $*" >&2; exit "$c"; }

valid_vm() {
    [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] || die 2 "bad VM name: $1"
}

# tart runs as an account with a uid we can name; uid 0 or no uid at all would
# turn `launchctl asuser` into a call that means something else.
need_run_uid() {
    [[ "$RUN_UID" =~ ^[0-9]+$ ]] && [[ "$RUN_UID" -gt 0 ]] \
        || die 3 "account $RUN_AS has no usable uid (got '${RUN_UID:-none}'); it must exist and not be root"
}

# Virtualization.framework boots a macOS guest only from a process in a GUI
# login session (outside one, tart run fails with VZErrorDomain Code=-9,
# "Failed to get current host key"; measured 2026-10-17, also as root).
# hellmini has FileVault, so no account logs in by itself; tart runs as the
# throwaway account that stays signed in there (nxmvv), inside its session.
# TART_HOME belongs to that account.
as_run() { sudo -n launchctl asuser "$RUN_UID" sudo -n -u "$RUN_AS" "$@"; }
t() { as_run env TART_HOME="$TART_HOME" "$TART" "$@"; }

# tart's NAT is vmnet shared mode: 192.168.64.0/24, the host at .1. An address
# anywhere else is not our guest, and the key is not offered to it.
guest_ip() {
    local ip
    ip="$(t ip "$1" --wait 5 </dev/null 2>/dev/null)" || return 1
    if [[ "$ip" =~ ^192\.168\.64\.([2-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-4])$ ]]; then
        echo "$ip"; return 0
    fi
    [[ -z "$ip" ]] || echo "host.sh: tart ip for $1 is '$ip', outside tart's vmnet range 192.168.64.0/24; not connecting" >&2
    return 1
}

stat_fmt() {  # stat_fmt PATH: "uid octal-mode" (sticky bit included), BSD or GNU stat
    if stat --version >/dev/null 2>&1; then stat -c '%u %a' "$1" 2>/dev/null; else stat -f '%u %Lp' "$1" 2>/dev/null; fi
}
stat_mtime() {
    if stat --version >/dev/null 2>&1; then stat -c %Y "$1" 2>/dev/null; else stat -f %m "$1" 2>/dev/null; fi
}

group_alive() {  # group_alive PGID: any member alive; EPERM counts as alive (build-lease.sh)
    local err rc
    [[ "$1" =~ ^[0-9]+$ ]] || return 1
    err="$(LC_ALL=C kill -0 -- "-$1" 2>&1)"; rc=$?
    [[ $rc -eq 0 ]] && return 0
    case "$err" in *"not permitted"*|*EPERM*) return 0 ;; esac
    return 1
}

# lease_busy DIR: 0 when the lease at DIR is held, or cannot be read (a holder whose
# files we may not read looks exactly like a corpse, and guessing "corpse" would
# start a guest under a running suite: tests/_suite_lease.py _UNREADABLE_HOLDER);
# 1 when it is free or stale. Says why on stdout.
lease_busy() {
    local dir="$1" name pid pgid age
    name="${dir##*/}"
    [[ -e "$dir" || -L "$dir" ]] || return 1
    if [[ ! -d "$dir" || ! -r "$dir" || ! -x "$dir" ]]; then
        echo "busy: lease $name at $dir cannot be read (treated as held)"; return 0
    fi
    pid=''
    if [[ -e "$dir/pid" || -L "$dir/pid" ]]; then
        if ! pid="$(cat "$dir/pid" 2>/dev/null)"; then
            echo "busy: lease $name at $dir has a pid file that cannot be read (treated as held)"; return 0
        fi
    fi
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
        if ps -p "$pid" >/dev/null 2>&1; then
            echo "busy: pid $pid holds nexus-build-lease/$name"; return 0
        fi
        pgid="$(cat "$dir/pgid" 2>/dev/null || true)"
        if [[ -n "$pgid" ]] && group_alive "$pgid"; then
            echo "busy: process group $pgid of nexus-build-lease/$name is alive"; return 0
        fi
        return 1
    fi
    # No usable pid: another acquirer between its mkdir and its pid write holds it
    # for a minute (PIDLESS_GRACE_SECONDS); after that it is a corpse.
    age=$(( $(date +%s) - $(stat_mtime "$dir" || echo 0) ))
    if [[ $age -lt 60 ]]; then
        echo "busy: lease $name has no pid yet and is $age s old (being populated)"; return 0
    fi
    return 1
}

# The suite and engine-build leases of the clone's git common dir
# (scripts/lib/build-lease.sh: nexus-build-lease/<name>/pid), a Runner.Worker of
# either runner user, and any tart VM or `tart run` of ours are what claim
# hellmini's RAM.
cmd_guard() {
    local common root busy=0 name u checked=0 rc why vms
    common="$LEASE_COMMON"
    if [[ -z "$common" ]]; then
        common="$(git -C "$LEASE_REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
            || die 3 "cannot find the git common dir of $LEASE_REPO (lease check impossible)"
    fi
    root="$common/nexus-build-lease"
    if [[ -e "$root" ]] && [[ ! -d "$root" || ! -r "$root" || ! -x "$root" ]]; then
        echo "host.sh: busy: the lease root $root cannot be read (treated as held)" >&2; busy=1
    else
        for name in suite service; do
            if why="$(lease_busy "$root/$name")"; then echo "host.sh: $why" >&2; busy=1; fi
        done
    fi
    for u in $RUNNER_USERS; do
        if ! id -u "$u" >/dev/null 2>&1; then
            echo "guard: runner user $u does not exist; not checked"; continue
        fi
        checked=$((checked + 1))
        pgrep -u "$u" -f Runner.Worker >/dev/null 2>&1; rc=$?
        case $rc in
            0) echo "host.sh: busy: runner user $u has a Runner.Worker (a CI job is running)" >&2; busy=1 ;;
            1) ;;
            *) echo "host.sh: busy: pgrep for runner user $u failed (exit $rc); treated as a running job" >&2; busy=1 ;;
        esac
    done
    [[ $checked -gt 0 ]] || die 3 "none of the runner users ($RUNNER_USERS) exists; the CI-job check would pass without looking at anything"
    pgrep -f '(^|[/ ])tart run( |$)' >/dev/null 2>&1; rc=$?
    case $rc in
        0) echo "host.sh: busy: a tart run process exists" >&2; busy=1 ;;
        1) ;;
        *) echo "host.sh: busy: pgrep for tart run failed (exit $rc)" >&2; busy=1 ;;
    esac
    need_run_uid
    if vms="$(t list -q --source local </dev/null 2>/dev/null)"; then
        if printf '%s\n' "$vms" | grep -E '^(nx-mac-run-|nexus-mac-build$)' >/dev/null; then
            echo "host.sh: busy: a tart VM of ours exists: $(printf '%s\n' "$vms" | grep -E '^(nx-mac-run-|nexus-mac-build$)' | tr '\n' ' ')" >&2; busy=1
        fi
    else
        echo "host.sh: busy: cannot list the tart VMs (treated as one running)" >&2; busy=1
    fi
    [[ $busy -eq 0 ]] || exit 75
    echo "guard: free (no lease holder, no Runner.Worker, no tart VM or tart run of ours; $checked runner user(s) checked)"
}

# check_chain PATH: PATH and every ancestor up to / is owned by the invoking user
# or root and is not writable by group or other (a sticky root-owned directory such
# as /tmp is the one exception: others cannot replace our entries there). Anyone who
# could write one of these could swap the key, the pin or this script for their own.
check_chain() {
    local cur="$1" me owner mode perm
    me="$(id -u)"
    [[ -e "$cur" ]] || die 3 "$1 does not exist"
    [[ -L "$cur" ]] && die 3 "$1 is a symlink; refusing it"
    if [[ -d "$cur" ]]; then cur="$(cd -P "$cur" && pwd -P)"; else cur="$(cd -P "$(dirname "$cur")" && pwd -P)/$(basename "$cur")"; fi
    while :; do
        [[ -n "$TEST_ROOT" && "$cur" == "$TEST_ROOT" ]] && break
        read -r owner mode <<<"$(stat_fmt "$cur")"
        [[ -n "${owner:-}" && -n "${mode:-}" ]] || die 3 "cannot stat $cur"
        if [[ "$owner" != 0 && "$owner" != "$me" ]]; then
            die 3 "$cur is owned by uid $owner (not this user and not root); fix with: sudo chown $me '$cur'"
        fi
        perm=$(( 8#$mode ))
        if (( perm & 8#022 )); then
            if ! { (( perm & 8#1000 )) && [[ "$owner" == 0 ]]; }; then
                die 3 "$cur is writable by group or other (mode $mode); fix with: chmod go-w '$cur'"
            fi
        fi
        [[ "$cur" == / ]] && break
        cur="$(dirname "$cur")"
    done
}

cmd_preflight() {
    local self
    self="$(cd -P "$(dirname "$0")" && pwd -P)/$(basename "$0")"
    [[ -x "$TART" ]] || die 3 "tart is not installed at $TART (README: host setup)"
    [[ -d "$BASE" ]] || die 3 "$BASE does not exist (run build-image.sh)"
    check_chain "$BASE"
    check_chain "$KEYDIR"
    check_chain "$self"
    [[ -f "$BASE/host.sh" ]] && check_chain "$BASE/host.sh"
    [[ -d "$BASE/runs" ]] && check_chain "$BASE/runs"
    [[ -f "$KEYDIR/id_ed25519" ]] || die 3 "no guest key at $KEYDIR/id_ed25519 (run build-image.sh)"
    check_chain "$KEYDIR/id_ed25519"
    [[ -s "$KEYDIR/known_hosts" ]] || die 3 "no pinned guest host key at $KEYDIR/known_hosts (run build-image.sh)"
    check_chain "$KEYDIR/known_hosts"
    need_run_uid
    sudo -n launchctl print "gui/$RUN_UID" >/dev/null 2>&1 \
        || die 3 "$RUN_AS (uid $RUN_UID) is not signed in at the hellmini login window; sign it in once (Screen Sharing)"
    t get "$CLEAN" >/dev/null 2>&1 || die 3 "no clean image $CLEAN in $TART_HOME (run build-image.sh)"
    echo "preflight: tart $(t --version) as $RUN_AS (GUI session gui/$RUN_UID), image $CLEAN, key and pinned host key present, files owner-checked"
}

run_alive() { pgrep -f "tart run --no-graphics $1\$" >/dev/null 2>&1; }

# vm_listed VM: 0 listed, 1 not listed, 2 tart cannot tell (a `tart get` failure
# would read "gone" when tart is merely unusable, so existence comes from the list).
vm_listed() {
    local l
    l="$(t list -q --source local </dev/null 2>/dev/null)" || return 2
    grep -qx -- "$1" <<<"$l"
}

stop_deadman() {
    local pf="$BASE/deadman/$1.pid" pid cmdline
    [[ -f "$pf" ]] || return 0
    pid="$(cat "$pf" 2>/dev/null || true)"
    # Not from inside the deadman itself, and only a process that still is one of ours.
    cmdline=''
    [[ "$pid" =~ ^[0-9]+$ ]] && cmdline="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    if [[ -z "${IN_DEADMAN:-}" && "$cmdline" == *host.sh* ]]; then
        pkill -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pf"
}

# The host-side deadman: whatever happens to the laptop, the ssh sessions and the
# hold, the VM is stopped and deleted by name when its lifetime is up.
start_deadman() {
    local vm="$1"
    mkdir -p "$BASE/deadman" "$BASE/logs"
    (
        trap '' HUP
        sleep "$LIFETIME_SECS"
        echo "deadman: lifetime cap ($LIFETIME_SECS s) reached for $vm; destroying it"
        IN_DEADMAN=1
        cmd_destroy "$vm"
    ) >>"$BASE/logs/$vm.deadman.log" 2>&1 </dev/null &
    echo $! >"$BASE/deadman/$vm.pid"
}

launch_tart_run() {  # launch_tart_run VM LOG
    nohup sudo -n launchctl asuser "$RUN_UID" sudo -n -u "$RUN_AS" env TART_HOME="$TART_HOME" "$TART" run --no-graphics "$1" >"$2" 2>&1 </dev/null &
}

cmd_start() {
    local vm="$1" src="${2:-$CLEAN}" log
    valid_vm "$vm"
    [[ "$vm" == nx-mac-run-* ]] || die 2 "start refuses $vm (only nx-mac-run-*)"
    t get "$vm" >/dev/null 2>&1 && die 1 "VM $vm already exists"
    t clone "$src" "$vm" || die 1 "tart clone $src $vm failed"
    log="$BASE/logs/$vm.run.log"
    mkdir -p "$BASE/logs"
    start_deadman "$vm"
    launch_tart_run "$vm" "$log"
    echo "started $vm from $src (tart run pid $!, log $log, deadman $LIFETIME_SECS s)"
}

cmd_waitssh() {
    local vm="$1" secs="$2" end ip seen=0 began
    valid_vm "$vm"
    began=$(date +%s)
    end=$(( began + secs ))
    while [[ $(date +%s) -lt $end ]]; do
        # start returns before the tart run process shows in the process table;
        # give it 15 s to appear, then an absent process is a dead VM.
        if run_alive "$vm"; then seen=1
        elif [[ $seen -eq 0 && $(( $(date +%s) - began )) -lt 15 ]]; then sleep 1; continue
        else die 1 "tart run for $vm is not running"; fi
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
    ip="$(guest_ip "$vm")"; [[ -n "$ip" ]] || die 1 "no usable address for $vm"
    exec ssh "${SSH_OPTS[@]}" "admin@$ip" "$@"
}

cmd_gscp() {
    local vm="$1" ip dest; shift
    valid_vm "$vm"
    dest="${*: -1}"; set -- "${@:1:$#-1}"
    ip="$(guest_ip "$vm")"; [[ -n "$ip" ]] || die 1 "no usable address for $vm"
    scp -q -r "${SSH_OPTS[@]}" "$@" "admin@$ip:$dest" </dev/null
}

cmd_gfetch() {
    local vm="$1" src="$2" dest="$3" ip
    valid_vm "$vm"
    ip="$(guest_ip "$vm")"; [[ -n "$ip" ]] || die 1 "no usable address for $vm"
    scp -q -r "${SSH_OPTS[@]}" "admin@$ip:$src" "$dest" </dev/null
}

# A guest-initiated reboot, and three things that together prove it: the boot
# session uuid changed, ssh was seen DOWN in between (a changed uuid alone could be
# a misread), and the same tart run still owns the VM (a VM that stopped instead of
# rebooting is a failure, not something to paper over with a cold start).
cmd_reboot() {
    local vm="$1" secs="$2" ip before after end down=0
    valid_vm "$vm"
    ip="$(guest_ip "$vm")"; [[ -n "$ip" ]] || die 1 "no usable address for $vm"
    before="$(ssh "${SSH_OPTS[@]}" "admin@$ip" 'sysctl -n kern.bootsessionuuid' </dev/null)" || die 1 "cannot read the boot session uuid"
    [[ "$before" =~ ^[0-9A-Fa-f-]{36}$ ]] || die 1 "kern.bootsessionuuid is not a uuid: '$before'"
    echo "reboot: boot session before: $before"
    ssh "${SSH_OPTS[@]}" "admin@$ip" 'sudo -n shutdown -r now' </dev/null >/dev/null 2>&1 || true
    end=$(( $(date +%s) + secs ))
    while [[ $(date +%s) -lt $end ]]; do
        run_alive "$vm" || die 1 "tart run for $vm exited during the reboot (the VM stopped instead of rebooting)"
        ip="$(guest_ip "$vm")"
        if [[ -n "$ip" ]] && after="$(ssh "${SSH_OPTS[@]}" "admin@$ip" 'sysctl -n kern.bootsessionuuid' </dev/null 2>/dev/null)"; then
            if [[ "$after" != "$before" && "$after" =~ ^[0-9A-Fa-f-]{36}$ ]]; then
                if [[ $down -eq 1 ]]; then
                    echo "reboot: boot session after: $after (ssh went down and came back at $ip)"; return 0
                fi
                die 1 "the boot session changed but ssh was never seen down; not accepted as a reboot"
            fi
        else
            down=1
        fi
        sleep 3
    done
    die 1 "the guest did not come back with a new boot session within $secs s (ssh down seen: $down)"
}

# build-image.sh only. The guest's host keys are unique to the image (provision.sh
# regenerates them); the pin is `* <key>` because the address changes per run.
# The key is read through the Tart guest agent, not over the network.
cmd_hostkey() {
    local vm="$1" line type key tmp
    valid_vm "$vm"
    [[ "$vm" == nexus-mac-build ]] || die 2 "hostkey pins only nexus-mac-build"
    line="$(t exec "$vm" /bin/cat /etc/ssh/ssh_host_ed25519_key.pub </dev/null 2>/dev/null)" || die 1 "cannot read the guest's ed25519 host key (guest agent)"
    read -r type key _ <<<"$line"
    [[ "$type" == ssh-ed25519 && "$key" =~ ^[A-Za-z0-9+/=]+$ ]] || die 1 "the guest's host key is not an ed25519 public key"
    mkdir -p "$KEYDIR"
    tmp="$KEYDIR/known_hosts.tmp.$$"
    if ! { printf '* %s %s\n' "$type" "$key" >"$tmp" && chmod 644 "$tmp" && mv "$tmp" "$KEYDIR/known_hosts"; }; then
        rm -f "$tmp"; die 1 "cannot write $KEYDIR/known_hosts"
    fi
    echo "pinned the guest host key ($type ${key:0:16}...) in $KEYDIR/known_hosts"
}

# build-image.sh only. The build VM gets the guest size every clone inherits.
cmd_build_start() {
    local src="$1" log
    t get nexus-mac-build >/dev/null 2>&1 && die 1 "nexus-mac-build already exists (host.sh destroy nexus-mac-build)"
    t clone "$src" nexus-mac-build || die 1 "tart clone $src nexus-mac-build failed"
    t set nexus-mac-build --cpu "$CPUS" --memory "$MEMORY_MB" || die 1 "tart set failed"
    log="$BASE/logs/nexus-mac-build.run.log"; mkdir -p "$BASE/logs"
    launch_tart_run nexus-mac-build "$log"
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

# Kill the tart run process of VM as the tart account, by exact command line.
kill_run() {
    local vm="$1" pat="tart run --no-graphics $1\$"
    as_run "$PKILL" -TERM -f "$pat" >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5; do run_alive "$vm" || return 0; sleep 1; done
    as_run "$PKILL" -KILL -f "$pat" >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5; do run_alive "$vm" || return 0; sleep 1; done
    return 1
}

# Close the ssh sessions into VM's guest. When only the laptop's side of a run is
# killed, the session on this host stays up until ssh notices the dead peer (up to
# two minutes) and would otherwise outlive the VM it points at.
reap_guest_ssh() {
    local ip
    ip="$(guest_ip "$1" 2>/dev/null)" || return 0
    pkill -TERM -f "ssh -i ${KEYDIR}/id_ed25519 .*admin@${ip//./[.]}( |\$)" 2>/dev/null || true
}

destroy_once() {
    local vm="$1" i rc=0
    vm_listed "$vm" || rc=$?
    [[ $rc -ne 2 ]] || return 1
    if [[ $rc -eq 0 ]]; then
        if run_alive "$vm"; then
            reap_guest_ssh "$vm"
            t stop "$vm" --timeout 60 >/dev/null 2>&1 || true
            for ((i = 0; i < STOP_WAIT; i += 2)); do run_alive "$vm" || break; sleep 2; done
            if run_alive "$vm"; then
                echo "host.sh: $vm is still running after tart stop; killing its tart run" >&2
                kill_run "$vm" || return 1
            fi
        fi
        t delete "$vm" >/dev/null 2>&1 || return 1
    fi
    rc=0; vm_listed "$vm" || rc=$?
    [[ $rc -eq 1 ]]
}

cmd_destroy() {
    local vm="$1" attempt
    valid_vm "$vm"
    [[ "$vm" == nx-mac-run-* || "$vm" == nexus-mac-build ]] || die 2 "destroy refuses $vm (only nx-mac-run-* and nexus-mac-build)"
    for attempt in 1 2 3; do
        if destroy_once "$vm"; then
            stop_deadman "$vm"
            echo "destroyed $vm"
            return 0
        fi
        echo "host.sh: destroy attempt $attempt of 3 for $vm failed" >&2
        [[ $attempt -lt 3 ]] && sleep "$RETRY_SLEEP"
    done
    die 1 "cannot destroy $vm after 3 attempts (the deadman keeps trying until its cap)"
}

# Hold the suite lease for the whole life of a run. A suite started meanwhile
# finds it held and exits 75 (tests/_suite_lease.py), and the runner overlap guard
# waits. The lease is released only after the VM is gone.
#
# Signals only set a flag: the loop notices it within 3 s and the cleanup runs in
# the ordinary flow with the signals already ignored, and destroys the VM in a child
# process. Doing this from a trap, with a subshell and `trap ''` inside it, put the
# macOS /bin/bash (3.2) into a signal-handler loop when a second signal arrived
# during the cleanup (measured 2026-10-17: the process spun at 100% CPU, lease held).
HOLD_SIG=''
HOLD_DIR=''
hold_end() {
    local why="$1"
    trap '' INT TERM HUP
    trap - EXIT
    echo "HOLD: ending ($why); destroying $HOLD_VM before the lease is released"
    if ! "$HOLD_SELF" destroy "$HOLD_VM"; then
        echo "HOLD: WARNING $HOLD_VM was not destroyed; the deadman retries until its cap"
    fi
    build_lease_release suite
    echo "HOLD: released the suite lease"
    [[ -z "$HOLD_DIR" ]] || rm -rf "$HOLD_DIR"
}

cmd_hold() {
    local vm="$1" keep="${2:-}" lib rc start t0 why=''
    valid_vm "$vm"
    [[ "$vm" == nx-mac-run-* ]] || die 2 "hold refuses $vm (only nx-mac-run-*)"
    [[ -z "$keep" || "$keep" == keep ]] || die 2 "hold VM [keep]"
    lib="$LEASE_REPO/scripts/lib/build-lease.sh"
    [[ -r "$lib" ]] || die 3 "no lease library at $lib"
    [[ -z "$LEASE_COMMON" ]] || export NX_BUILD_LEASE_ROOT="$LEASE_COMMON/nexus-build-lease"
    # shellcheck source=/dev/null
    source "$lib"
    need_run_uid
    HOLD_VM="$vm"
    # launch.sh starts this as $RD/host.sh and removes $RD when it ends, and a kept hold
    # outlives that, so hold_end destroys through a private copy (nexus-2fn57: the destroy at
    # the cap exited 127). mktemp -d makes the directory 0700.
    HOLD_DIR="$(mktemp -d)" || die 3 "hold: cannot make a private directory"
    HOLD_SELF="$HOLD_DIR/host.sh"
    if ! cp "$(cd -P "$(dirname "$0")" && pwd -P)/$(basename "$0")" "$HOLD_SELF" || ! chmod 700 "$HOLD_SELF"; then
        rm -rf "$HOLD_DIR"; die 3 "hold: cannot copy host.sh to $HOLD_DIR"
    fi
    rc=0
    build_lease_acquire suite "mac-guest $vm" || rc=$?
    if [[ $rc -ne 0 ]]; then
        rm -rf "$HOLD_DIR"
        echo "host.sh: hold: the suite lease is held or unavailable (exit $rc)" >&2; exit "$rc"
    fi
    trap 'HOLD_SIG=TERM' TERM
    trap 'HOLD_SIG=INT' INT
    trap 'HOLD_SIG=HUP' HUP
    # An exit from anywhere else (a bug, set -u) still destroys and releases.
    trap 'hold_end "unexpected exit"' EXIT
    echo "HOLD: acquired the suite lease (pid $$) for $vm"
    start=$SECONDS
    while [[ -z "$HOLD_SIG" ]] && (( SECONDS - start < LIFETIME_SECS )); do
        # /bin/bash here is 3.2, where `read -t` returns 1 for a timeout as it does
        # for EOF. A timeout takes the full 3 s and an EOF returns at once, so the
        # clock tells them apart (a timeout is never under 2 whole seconds).
        t0=$SECONDS
        if read -r -t 3 _; then continue; fi
        (( SECONDS - t0 >= 2 )) && continue      # timeout: nothing yet
        [[ -z "$HOLD_SIG" ]] || continue          # a signal interrupted the read
        # EOF: the laptop is done (or gone).
        echo "HOLD: stdin closed"
        why="stdin closed"
        if [[ "$keep" == keep ]]; then
            echo "HOLD: keeping $vm and the lease until it is deleted or the cap"
            while [[ -z "$HOLD_SIG" ]] && (( SECONDS - start < LIFETIME_SECS )); do
                vm_listed "$vm"; [[ $? -eq 1 ]] && break
                sleep 5
            done
        fi
        break
    done
    if [[ -n "$HOLD_SIG" ]]; then why="got $HOLD_SIG"
    elif [[ -z "$why" ]]; then why="lifetime cap"; fi
    hold_end "$why"
    case "$HOLD_SIG" in TERM) exit 143 ;; INT) exit 130 ;; HUP) exit 129 ;; esac
}

verb="${1:-}"; shift || true
case "$verb" in
    preflight|hold|start|waitssh|gssh|gscp|gfetch|reboot|destroy|hostkey|build-start|build-finish|tart) need_run_uid ;;
esac
case "$verb" in
    guard) cmd_guard ;;
    preflight) cmd_preflight ;;
    hold) [[ $# -ge 1 && $# -le 2 ]] || die 2 "hold VM [keep]"; cmd_hold "$@" ;;
    start) [[ $# -ge 1 ]] || die 2 "start VM [SRC]"; cmd_start "$@" ;;
    waitssh) [[ $# -eq 2 ]] || die 2 "waitssh VM SECS"; cmd_waitssh "$@" ;;
    gssh) [[ $# -ge 2 ]] || die 2 "gssh VM CMD..."; cmd_gssh "$@" ;;
    gscp) [[ $# -ge 3 ]] || die 2 "gscp VM SRC... DEST"; cmd_gscp "$@" ;;
    gfetch) [[ $# -eq 3 ]] || die 2 "gfetch VM SRC DEST"; cmd_gfetch "$@" ;;
    reboot) [[ $# -eq 2 ]] || die 2 "reboot VM SECS"; cmd_reboot "$@" ;;
    destroy) [[ $# -eq 1 ]] || die 2 "destroy VM"; cmd_destroy "$@" ;;
    hostkey) [[ $# -eq 1 ]] || die 2 "hostkey nexus-mac-build"; cmd_hostkey "$@" ;;
    build-start) [[ $# -eq 1 ]] || die 2 "build-start SRC"; cmd_build_start "$@" ;;
    build-finish) cmd_build_finish ;;
    tart) t "$@" ;;
    *) die 2 "unknown verb '$verb' (guard preflight hold start waitssh gssh gscp gfetch reboot destroy hostkey)" ;;
esac
