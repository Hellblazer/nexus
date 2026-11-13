#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Run the RDR-224 Phase 5 gate (tests/e2e/windows-phase5-gate.ps1) unattended in
# the Windows gate guest, from the Mac, and print the verdict line. Exits 0 only
# when the collected verdict line is "WINDOWS PHASE 5 GATE PASSED
# (executed=N declared=N)" with N equal, N above zero, and the gate's own line
# "gate script sha256 <sha> (...; the setup phase ran the same file)" in the same
# verdict.txt carries the sha256 of the gate script this run staged; every other
# outcome exits non-zero. The one exception is --build-signed-in, which runs no
# gate: it exits 0 only after its checkpoint is taken, the guest's CheckpointType
# is back as found and the end step finished clean, prints its own line ("SIGNED-IN
# CHECKPOINT BUILT (no gate was run; this is not a gate verdict)") and never a
# verdict line.
#
#   tests/e2e/windows-guest/run-gate.sh --version X.Y.Z --engine A.B.C \
#       [--wheel PATH] [--out DIR] [--keep-guest] [--cache on|off] [--standard-user [--no-admin-git] [--applocker | --applocker-audit | --applocker-allow | --applocker-allow-dll-audit | --applocker-allow-dll | --wdac-audit | --wdac-allow] [--build-signed-in NAME | --signed-in NAME]]
#
# --version and --engine are the gate's ExpectedVersion and ExpectedEngine; there
# is no default for either. --wheel installs that candidate wheel instead of
# conexus==X.Y.Z from PyPI, and the run is then named a candidate wheel (not a
# published build) in GATERUN.txt and in the output. --out is where the logs and
# the collected run directory land (default: a new directory under $TMPDIR).
# --keep-guest leaves the guest running afterwards, for diagnosis; the host files
# are removed either way.
# --standard-user runs the install, the setup phase, the Claude Code session and
# the verify phase as a local account that is a member of Users only
# (NX_GUEST_STD_USER, default nxstd), which the stduser step creates after the
# restore and the next restore of the baseline erases (a --signed-in run resumes it). The administrator account ($GUSER) then
# only orchestrates, and installs Git for all users first, because winget's Git
# package needs elevation: the run is a standard user on a machine that already
# has Git. driver/stduser.ps1 lists everything the administrator does.
# --no-admin-git (with --standard-user) leaves Git out of that: the account
# installs PortableGit itself, by running the PowerShell block of
# docs/windows-install.md section 2 exactly as it is written there (this script
# cuts it out of the file at run time), so the run is a standard user on a
# machine with no Git following the install guide.
# --applocker (with --standard-user) has the administrator apply AppLocker's
# default rules, enforced, for executables, installers, scripts and packaged
# apps before the account exists: everyone may run what is under Program Files
# and Windows, administrators may run anything, and nothing else runs. The
# stduser step stops the run unless a program in the account's own profile is
# then denied for it, so a pass cannot come from a policy that never took
# effect. The run is a standard user on a machine with application control.
# --applocker-audit sets the same rules to audit only: nothing is blocked, and
# the AppLocker logs name every program (event 8003) and script (8006) the
# enforced rules would have refused, which is the list an administrator's allow
# rules have to cover. The step waits for the first such record instead.
# --applocker-allow enforces the executable, installer and packaged-app rules
# and adds path rules that let every user run programs from the folders this
# install uses under the profile (driver/stduser.ps1 lists them; they come from
# the audit run of 2026-10-13). The script rules are audit only in this mode:
# enforced, they put PowerShell in ConstrainedLanguage, where the uv installer
# script cannot run whatever the executable rules say.
# --applocker-allow-dll-audit is --applocker-allow plus AppLocker's DLL rule
# collection (default rules and the same folders) in audit only: nothing more is
# blocked, and the log names every DLL (event 8003) the DLL rules would refuse.
# --applocker-allow-dll enforces those DLL rules, with the folders outside the
# install's that the audit run recorded (driver/stduser.ps1 $dllExtra).
# --wdac-audit (with --standard-user --no-admin-git) deploys an App Control for
# Business (WDAC) policy built from Windows' DefaultWindows_Enforced example in
# audit mode: the CodeIntegrity log (event 3076) names every program and DLL the
# enforced policy would refuse. The policy trusts only code Windows signs, for
# every account, so a Git installed for all users under Program Files would be
# refused like anything else; hence --no-admin-git.
# --wdac-allow enforces it, with path rules for this install's folders under the
# account's profile, runtime file-path protection off (WDAC refuses rules for
# user-writable folders at run time otherwise) and script enforcement off
# (enforced, PowerShell runs in ConstrainedLanguage, as with AppLocker's script
# rules). The stduser step stops the run unless an unsigned control program
# started as the account is recorded (3077 blocked, 3076 in audit).
# --cache on (the default; nexus-bpzt7) saves what every restored guest would
# download again: the restore step turns Windows Update off in the guest for that
# boot, step 01c copies the host's C:\build\guest\cache (a uv cache archive, a
# Claude Code binary, nx init's two ONNX models when they were cached for
# this version, and the install step's own downloads: uv's zip, PortableGit or
# Git's installer, and the Python archive, each under its own version) into the
# installing account, and step 07 refills that cache after the run. The Claude
# Code binary, uv's zip, PortableGit and the Git installer are used only when
# their sha256 is the one the upstream publishes for that version, and anything
# else is downloaded as before (the Python archive must match the sha256
# sidecar written when it was harvested). uv still resolves conexus against
# PyPI and only the files come from the cache, so the install runs the published
# path; the nexus engine and PostgreSQL bundle are downloaded and checked by nx
# init as usual. Measured on Home: 2.46 GB per cycle before the
# cache (2026-10-15), 1.30 GB with it before the models were cached (2026-10-16,
# at the host's Default Switch). --cache off downloads everything
# and leaves Windows Update alone, for a run whose subject is the download path.
# --build-signed-in NAME (nexus-y3y8d; needs --standard-user) builds, and does not
# run the gate: the first sign-in of the standard user costs about 300 MiB of
# network a run, so one running (Standard, memory-state) checkpoint of the guest
# after that sign-in is taken once, and --signed-in resumes from it. The run is the
# usual hostcheck, restore (to the baseline checkpoint, as always) and stduser
# steps; then step 01k checkpoint waits a settle period (NX_GUEST_SETTLE_S seconds,
# default 600; the host's Default Switch byte counter is printed once a minute, for
# the record only) and takes the checkpoint NAME as a child of the baseline, and the
# run goes straight to the end step (the guest is shut down, the staged files
# removed): no install, setup, gate, launch, verdict or harvest, and no verdict line.
# NAME must match stduser-signed-in-<8 digits>. The checkpoint holds the default
# standard-user state only, so the flag refuses --applocker*, --wdac*, --no-admin-git,
# --wheel and --cache off (the quiet block the checkpoint carries is applied only
# with the cache on).
# Sam's exception (2026-11-13) is ONE running child checkpoint of the gate guest,
# and the code holds to exactly that, here and without this wrapper in hostcheck.ps1,
# checkpoint.ps1 and restore.ps1: --build-signed-in is refused unless the VM is
# nx-clean-win11 and the baseline checkpoint is clean-baseline-2026-10-06; it is
# refused, before anything is restored, when ANY checkpoint whose name matches
# ^stduser-signed-in- exists already (one checkpoint only: a rebuild needs Sam to
# delete the old one by hand, and the refusal says so); NX_GUEST_CHECKPOINT matching
# ^stduser-signed-in- is refused in every mode, and only --signed-in names such a
# checkpoint. No script removes, renames or replaces a checkpoint: the baseline is
# never removed, renamed or replaced.
# Account secret: the stduser step writes the account secret to Winlogon for its one restart and removes it before the checkpoint,
# and the build fails unless that step printed "winlogon secret removed=True"; no
# script keeps it after that. The checkpoint's
# MEMORY IMAGE (the .vmrs) does hold the signed-in session, with whatever the
# session keeps in memory, and the build's CheckpointType change is undone on every
# exit path: checkpoint.ps1 writes the type it found to a file in the run directory
# first, and end.ps1, which runs on every exit, puts it back from that file.
# --signed-in NAME (needs --standard-user; the same refusals) runs the gate from that
# checkpoint instead of the baseline's: hostcheck additionally refuses unless the guest
# has a checkpoint NAME whose parent is the baseline checkpoint (and prints its type,
# which is a reading and not a test: the baseline is also Standard); restore restores NAME and
# resumes the guest (restore.ps1 -Resume yes: the guest must be Saved after the restore,
# which is the real test of a running checkpoint; there is no offline boot, and the quiet
# block and the network wait run as always, with one `ipconfig /renew` in the guest when
# the network has not answered by the third try, and the guest's clock checked against
# the host's and resynced once); and the stduser step runs with -Existing yes: it creates
# no account, sets no policy, installs no Git, stores no secret and does not restart, but
# checks that the account exists, is a member of Users only, is signed in at the console
# with its desktop shell running, that the quiet-guest policy reads back, that Git is
# installed for all users and that Winlogon holds no secret, then stages the gate script
# in the account's profile. Every later step is as in a plain --standard-user run.
# Only this path may use that checkpoint; every other path restores the baseline.
#
# A --signed-in RUN IS A DIFFERENT CLAIM from a run restored to clean-baseline-2026-10-06.
# It does not exercise the standard user's first sign-in (the account, its profile and its
# first-logon work are already in the checkpoint) and it does not exercise the boot after a
# restore (the guest resumes from saved memory instead of booting; the offline boot and the
# fresh network start are skipped). A gate record that cites clean-baseline-2026-10-06 as
# its starting state must not cite a --signed-in run; cite the checkpoint name and this
# limit. The run says so itself, at its start and in its summary.
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
#                again on the guest. It prints "NXG-RESTORED: ok" the moment the
#                restore succeeds; this script treats the guest as its own (and
#                collects from it and powers it off at the end) only when that
#                line is in the step's output.
#   1s stduser   (--standard-user only) driver/stduser.ps1
#   1k checkpoint (--build-signed-in only) driver/checkpoint.ps1: settle, then take the
#                running checkpoint NAME; the run then skips to the end step (no gate
#                verdict, no collection)
#   1c seed      (--cache on) driver/cache.ps1 -Mode seed
#   2 install    uv, conexus, Claude Code, Git, the plugin
#   3 setup      the gate's setup phase, a one-shot Interactive scheduled task
#   4 gaterun    write GATERUN.txt
#   5 launch     launch.sh: a signed-in Claude Code session that follows it
#   6 verdict    bounded wait for verdict.txt, read-only collection of the run
#   7 harvest    (--cache on) driver/cache.ps1 -Mode harvest; not part of the
#                verdict, a failure is only reported
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
#   NX_GUEST_CHECKPOINT. NX_GUEST_TIMEOUT_<STEP> (seconds) changes a step's bound;
#   see "Bounds" below. NX_GUEST_SETTLE_S (seconds, default 600, leading zeros read as decimal)
#   is --build-signed-in's settle period.
#
# Result channel: the remote login shell is PowerShell, so a non-zero exit of a
# host script reaches this side as 1 whatever it was. Only zero versus non-zero
# survives ssh; the "NXG-RESULT:" lines in each step's log are the channel that
# says what happened, and run_step stops on a missing "NXG-RESULT: ok".
#
# Bounds: every step is cut off after a fixed number of seconds (the install,
# setup and verdict steps get the most), and so are the collection and the end
# step. A bound that fires kills the local ssh and fails the run. A dropped ssh
# during the 27-minute verdict wait fails the run the same way, and the end step
# then powers the guest off mid-verify (the run's state stays on the guest disk;
# --keep-guest keeps it up).
#
# Signals: INT, TERM and HUP run the cleanup (collect, end step) once, then exit
# 130, 143 or 129. A signal that arrives during the cleanup itself aborts only the
# stage it hit; the end step still runs, once.
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
STD_USER="${NX_GUEST_STD_USER:-nxstd}"
KEYCHAIN_SERVICE="${NX_GUEST_KEYCHAIN_SERVICE:-nexus-win-guest}"
CHECKPOINT="${NX_GUEST_CHECKPOINT:-clean-baseline-2026-10-06}"
export NX_GUEST_HOST="$HOST" NX_GUEST_VM="$VM" NX_GUEST_USER="$GUSER" NX_GUEST_KEYCHAIN_SERVICE="$KEYCHAIN_SERVICE"

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
gate="$repo/tests/e2e/windows-phase5-gate.ps1"
driver="$here/driver"

usage() {
    echo "usage: run-gate.sh --version X.Y.Z --engine A.B.C [--wheel PATH] [--out DIR] [--keep-guest] [--cache on|off] [--standard-user [--no-admin-git] [--applocker | --applocker-audit | --applocker-allow | --applocker-allow-dll-audit | --applocker-allow-dll | --wdac-audit | --wdac-allow] [--build-signed-in NAME | --signed-in NAME]]" >&2
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
standard_user=0
admin_git=yes
applocker=no
wdac=no
cache=on
signed_in_build=''
signed_in=''
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) [[ $# -ge 2 ]] || { usage; exit 2; }; version="$2"; shift 2 ;;
        --engine) [[ $# -ge 2 ]] || { usage; exit 2; }; engine="$2"; shift 2 ;;
        --wheel) [[ $# -ge 2 ]] || { usage; exit 2; }; wheel="$2"; shift 2 ;;
        --out) [[ $# -ge 2 ]] || { usage; exit 2; }; out="$2"; shift 2 ;;
        --keep-guest) keep_guest=1; shift ;;
        --cache) [[ $# -ge 2 ]] || { usage; exit 2; }; cache="$2"; shift 2 ;;
        --standard-user) standard_user=1; shift ;;
        --no-admin-git) admin_git=no; shift ;;
        --applocker) applocker=yes; shift ;;
        --applocker-audit) applocker=audit; shift ;;
        --applocker-allow) applocker=allow; shift ;;
        --applocker-allow-dll-audit) applocker=allow-dll-audit; shift ;;
        --applocker-allow-dll) applocker=allow-dll; shift ;;
        --wdac-audit) wdac=audit; shift ;;
        --wdac-allow) wdac=allow; shift ;;
        --build-signed-in) [[ $# -ge 2 ]] || { usage; exit 2; }; signed_in_build="$2"; shift 2 ;;
        --signed-in) [[ $# -ge 2 ]] || { usage; exit 2; }; signed_in="$2"; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -n "$version" && -n "$engine" ]] || { usage; die 2 "--version and --engine are required"; }
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 2 "--version must be X.Y.Z"
[[ "$engine" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 2 "--engine must be X.Y.Z"
[[ "$cache" == on || "$cache" == off ]] || { usage; die 2 "--cache must be on or off"; }
# These values reach a remote command line; keep them to plain tokens.
for pair in "NX_GUEST_HOST:$HOST" "NX_GUEST_VM:$VM" "NX_GUEST_USER:$GUSER" "NX_GUEST_KEYCHAIN_SERVICE:$KEYCHAIN_SERVICE" "NX_GUEST_CHECKPOINT:$CHECKPOINT" "NX_GUEST_STD_USER:$STD_USER"; do
    [[ "${pair#*:}" =~ ^[A-Za-z0-9._-]+$ ]] || die 2 "${pair%%:*} must be plain letters, digits, dot, dash or underscore"
done
[[ "$admin_git" == yes || $standard_user -eq 1 ]] || { usage; die 2 "--no-admin-git needs --standard-user"; }
[[ "$applocker" == no || $standard_user -eq 1 ]] || { usage; die 2 "the --applocker modes need --standard-user"; }
[[ "$wdac" == no || ( $standard_user -eq 1 && "$admin_git" == no ) ]] || { usage; die 2 "the --wdac modes need --standard-user --no-admin-git"; }
[[ "$wdac" == no || "$applocker" == no ]] || { usage; die 2 "a --wdac mode and an --applocker mode are separate runs"; }
# nexus-y3y8d: the checkpoint after the standard user's first sign-in. Both flags hold the default
# standard-user state only. NX_GUEST_CHECKPOINT never names a signed-in checkpoint, in any mode: only
# --signed-in names one, and it names it itself.
[[ ! "$CHECKPOINT" =~ ^stduser-signed-in- ]] || die 2 "NX_GUEST_CHECKPOINT must not be a stduser-signed-in-* checkpoint: only --signed-in NAME names one"
SETTLE_S="${NX_GUEST_SETTLE_S:-600}"
[[ "$SETTLE_S" =~ ^[0-9]+$ ]] || die 2 "NX_GUEST_SETTLE_S must be a number of seconds"
SETTLE_S=$((10#$SETTLE_S))
[[ -z "$signed_in_build" || -z "$signed_in" ]] || { usage; die 2 "--build-signed-in and --signed-in are separate runs"; }
signed_in_name="${signed_in_build:-$signed_in}"
if [[ -n "$signed_in_name" ]]; then
    [[ "$signed_in_name" =~ ^stduser-signed-in-[0-9]{8}$ ]] || { usage; die 2 "the checkpoint name must match stduser-signed-in-<8 digits>"; }
    [[ $standard_user -eq 1 ]] || { usage; die 2 "--build-signed-in and --signed-in need --standard-user"; }
    [[ "$applocker" == no && "$wdac" == no ]] || { usage; die 2 "--build-signed-in and --signed-in hold the default standard-user state: no --applocker* or --wdac* mode"; }
    [[ "$admin_git" == yes ]] || { usage; die 2 "--build-signed-in and --signed-in hold Git for all users: no --no-admin-git"; }
    [[ -z "$wheel" ]] || { usage; die 2 "--build-signed-in and --signed-in take no --wheel"; }
    [[ "$cache" == on ]] || { usage; die 2 "--build-signed-in and --signed-in need --cache on (the default): the checkpoint carries the cache-on quiet block, and a resumed run would not be a cache-off run"; }
fi
# Sam's exception (2026-11-13): ONE running child checkpoint of nx-clean-win11's baseline, nothing else.
if [[ -n "$signed_in_build" ]]; then
    [[ "$VM" == nx-clean-win11 ]] || die 2 "--build-signed-in is allowed only for the VM nx-clean-win11 (NX_GUEST_VM is $VM)"
    [[ "$CHECKPOINT" == clean-baseline-2026-10-06 ]] || die 2 "--build-signed-in is allowed only as a child of the baseline checkpoint clean-baseline-2026-10-06 (NX_GUEST_CHECKPOINT is $CHECKPOINT)"
fi
install_doc="$repo/docs/windows-install.md"
[[ "$STD_USER" != "$GUSER" ]] || die 2 "NX_GUEST_STD_USER must not be the administrator account"
[[ -f "$gate" ]] || die 2 "no gate script at $gate"

# sha256_of <file> <variable>: the file's sha256 into the variable, or stop. An empty
# or malformed hash would make the greps below match any hash line at all.
sha256_of() {
    local s=''
    if command -v shasum >/dev/null 2>&1; then
        s="$(shasum -a 256 "$1" 2>/dev/null | awk '{print $1}')"
    elif command -v sha256sum >/dev/null 2>&1; then
        s="$(sha256sum "$1" 2>/dev/null | awk '{print $1}')"
    fi
    [[ "$s" =~ ^[0-9a-f]{64}$ ]] || die 2 "cannot compute a 64-hex sha256 of $1 (needs shasum or sha256sum)"
    printf -v "$2" '%s' "$s"
}

wheel_name=''
wheel_sha=''
if [[ -n "$wheel" ]]; then
    [[ -f "$wheel" ]] || die 2 "no such wheel: $wheel"
    wheel_name="$(basename "$wheel")"
    [[ "$wheel_name" =~ ^[A-Za-z0-9._+-]+\.whl$ ]] || die 2 "the wheel's file name has a character outside A-Za-z0-9._+-"
    [[ "$wheel_name" == "conexus-${version}-"* ]] || die 2 "the wheel is $wheel_name, not a conexus-${version}- wheel"
    sha256_of "$wheel" wheel_sha
fi
gate_sha=''
sha256_of "$gate" gate_sha
restore_cache=yes
[[ "$cache" == on ]] || restore_cache=no
cache_key="$version"
cache_note="--cache off: everything downloaded fresh"
[[ "$cache" == off ]] || cache_note="--cache on: Windows Update off in the guest, the uv package cache, Claude Code, uv itself, PortableGit and Python from the host cache when it held them and their sha256 matched, and winget's Git replaced by a direct silent run of the cached Git installer when its sha256 matches winget's (01c-seed.txt, 02-install.txt), the nx init models from the host cache when it held them for this version, nexus engine and bundle downloaded"
[[ -z "$wheel_sha" ]] || cache_key="w-${wheel_sha:0:16}"

RUN="nxg-$(date -u +%Y%m%dT%H%M%SZ)-$$"
HOST_WIN_DIR="C:\\build\\guest\\$RUN"
HOST_DIR="C:/build/guest/$RUN"
out="${out:-${TMPDIR:-/tmp}/nxgate-run-$RUN}"
mkdir -p "$out" || die 2 "cannot create $out"
[[ -z "$(ls -A "$out")" ]] || die 2 "$out is not empty"

# The PortableGit block of the install guide, cut out as written: the first
# powershell fence that names PortableGit. A guide that no longer has one stops
# the run here, before anything remote.
if [[ "$admin_git" == no ]]; then
    awk '/^```powershell/ { buf = ""; inb = 1; next }
         /^```/ { if (inb && buf ~ /PortableGit/) { printf "%s", buf; found = 1; exit } inb = 0; next }
         inb { buf = buf $0 "\n" }
         END { if (!found) exit 1 }' "$install_doc" >"$out/portable-git.ps1" \
        || die 2 "no powershell block naming PortableGit in $install_doc"
    grep -q "SetEnvironmentVariable('Path'" "$out/portable-git.ps1" || die 2 "the PortableGit block in $install_doc no longer sets the user PATH"
    LC_ALL=C grep -q '[^[:print:][:space:]]' "$out/portable-git.ps1" && die 2 "the PortableGit block in $install_doc has a non-ASCII character, which Windows PowerShell 5.1 would misread"
fi

say() { echo "run-gate.sh: $(date -u +%H:%M:%S) $*"; }
quiet() { grep -v -i -E 'post-quantum|store now, decrypt later|pq\.html' || true; }

# BatchMode: a key or host-key prompt would read the terminal and hang an unattended run.
SSH_OPTS=(-o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=6)

# Per-step bounds in seconds, each overridable as NX_GUEST_TIMEOUT_<STEP>.
bound() {
    local v="${!1:-$2}"
    [[ "$v" =~ ^[0-9]+$ && "$v" -gt 0 ]] || die 2 "$1 must be a positive number of seconds"
    printf -v "$3" '%s' "$v"
}
bound NX_GUEST_TIMEOUT_HOSTCHECK 300 T_HOSTCHECK
bound NX_GUEST_TIMEOUT_RESTORE 900 T_RESTORE
bound NX_GUEST_TIMEOUT_STDUSER 1200 T_STDUSER
bound NX_GUEST_TIMEOUT_CHECKPOINT $((SETTLE_S + 900)) T_CHECKPOINT
bound NX_GUEST_TIMEOUT_INSTALL 1800 T_INSTALL
bound NX_GUEST_TIMEOUT_CACHE 900 T_CACHE
bound NX_GUEST_TIMEOUT_SETUP 1800 T_SETUP
bound NX_GUEST_TIMEOUT_GATERUN 300 T_GATERUN
bound NX_GUEST_TIMEOUT_LAUNCH 600 T_LAUNCH
bound NX_GUEST_TIMEOUT_VERDICT 1800 T_VERDICT
bound NX_GUEST_TIMEOUT_COLLECT 600 T_COLLECT
bound NX_GUEST_TIMEOUT_END 300 T_END

# descendants <pid>: the pids below it, deepest first.
descendants() {
    local c
    for c in $(ps -A -o pid= -o ppid= 2>/dev/null | awk -v p="$1" '$2 == p {print $1}'); do
        descendants "$c"
        echo "$c"
    done
}

# run_bounded <seconds> <command...>: runs the command in the foreground (so a
# terminal signal reaches it) with a watchdog that, after <seconds>, kills what the
# command started. Returns the command's status, or 124 when the bound fired.
run_bounded() {
    local secs="$1" me wd rc flag
    shift
    me="$(exec sh -c 'echo $PPID')"
    flag="${TMPDIR:-/tmp}/nxgate-timeout.$$.$me.$RANDOM"
    rm -f "$flag"
    (
        self="$(exec sh -c 'echo $PPID')"
        n=0
        while [[ $n -lt $secs ]]; do
            sleep 1
            kill -0 "$me" 2>/dev/null || exit 0
            n=$((n + 1))
        done
        : >"$flag"
        # shellcheck disable=SC2046
        kill -TERM $(descendants "$me" | grep -v -x -e "$self") 2>/dev/null
    ) >/dev/null 2>&1 &
    wd=$!
    "$@"
    rc=$?
    kill "$wd" 2>/dev/null
    wait "$wd" 2>/dev/null
    if [[ -e "$flag" ]]; then
        rm -f "$flag"
        echo "run-gate.sh: timed out after $secs s: $1" >&2
        return 124
    fi
    return "$rc"
}

# A host script that needs no guest credential. stdin is /dev/null so that ssh
# cannot swallow the caller's.
host_ps() {
    local script="$1"; shift
    ssh "${SSH_OPTS[@]}" "$HOST" "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $HOST_WIN_DIR\\$script $*" </dev/null
}
# A host script that opens the guest: the password goes on stdin, line 1, and
# nowhere else.
host_ps_pw() {
    local script="$1"; shift
    security find-generic-password -a "$GUSER" -s "$KEYCHAIN_SERVICE" -w \
        | ssh "${SSH_OPTS[@]}" "$HOST" "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $HOST_WIN_DIR\\$script $*"
}
# The account whose profile holds nx-gate: the standard user once the stduser
# step has made it, the administrator otherwise. run_as is the matching
# guest-run.ps1 argument.
home_user="$GUSER"
run_as=''
launch_guest() {
    NX_GUEST_RUN_AS="${run_as:+$STD_USER}" "$here/launch.sh" --stage "$gate" --permission-mode auto \
        --prompt "follow the instructions in C:\\Users\\$home_user\\nx-gate\\GATERUN.txt" </dev/null
}

host_dir_made=0
collected=0
collect_started=0
end_started=0
end_warning=''
end_warned=0

# run_step <nn> <label> <seconds> <require-ok-marker 0|1> <command...>: runs the
# command for at most <seconds>, logs its output to $out/<nn>-<label>.txt, and
# stops the run on a non-zero exit (124: the bound fired) or, when asked, on a
# missing "NXG-RESULT: ok" line.
run_step() {
    local n="$1" label="$2" secs="$3" marker="$4" log rc
    shift 4
    log="$out/$n-$label.txt"
    say "step $n $label"
    echo "$label start $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee "$log"
    run_bounded "$secs" "$@" 2>&1 | quiet | tee -a "$log"
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
    run_bounded "$T_COLLECT" scp "${SSH_OPTS[@]}" -q -r "$HOST:$HOST_DIR/out" "$out/" 2>&1 | quiet
    if [[ -d "$out/out" ]]; then mv "$out/out" "$out/run"; collected=1; fi
}

# Always runs: a best-effort collection when this run restored the guest and
# nothing was collected yet, then the shutdown and the removal of the staged host
# files. Each stage runs at most once, so a second call (a signal during the first)
# resumes with what has not been tried. While it runs, INT, TERM and HUP only abort
# the stage they hit (the stage's child gets the same signal from the terminal):
# exiting here would skip the end step.
finish() {
    trap ':' INT TERM HUP
    [[ $host_dir_made -eq 1 ]] || return 0
    # "Restored" is evidence, not intent: the restore step's own marker line, printed
    # right after Restore-VMSnapshot succeeded. A restore that refused or failed
    # before that (the guest is another run's, say) leaves a guest this run must
    # neither read from nor power off.
    local touched=0 keep=''
    if [[ -f "$out/01-restore.txt" ]] && grep -q '^NXG-RESTORED: ok' "$out/01-restore.txt"; then touched=1; fi
    if [[ $touched -eq 1 && $collected -eq 0 && $collect_started -eq 0 ]]; then
        collect_started=1
        say "collecting what the guest holds"
        # shellcheck disable=SC2086
        { run_bounded "$T_COLLECT" host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Collect yes $run_as 2>&1 | quiet; } >"$out/90-collect-on-exit.txt" 2>&1 || true
        fetch_collected || true
    fi
    [[ $end_started -eq 0 ]] || return 0
    end_started=1
    # Never shut down a guest this run did not restore, and not when asked to keep it.
    if [[ $keep_guest -eq 1 || $touched -eq 0 ]]; then keep='-KeepGuest'; fi
    say "cleaning up the host"
    local wheel_arg='' end_rc
    if [[ -n "$wheel_name" ]]; then wheel_arg="-Wheel $wheel_name"; fi
    # shellcheck disable=SC2086
    run_bounded "$T_END" host_ps end.ps1 -VMName "$VM" -RunDir "$HOST_WIN_DIR" $wheel_arg $keep 2>&1 | quiet | tee "$out/99-end.txt"
    end_rc=${PIPESTATUS[0]}
    if [[ $end_rc -ne 0 ]] || ! grep -q '^NXG-RESULT: ok' "$out/99-end.txt"; then
        end_warning="the end step did not report NXG-RESULT: ok (exit $end_rc); the guest may still be running and files may be left in $HOST_DIR on $HOST; see $out/99-end.txt"
    fi
}
# Printed last, once, so that it is the line the reader ends on.
warn_end() {
    [[ -n "$end_warning" && $end_warned -eq 0 ]] || return 0
    end_warned=1
    echo "run-gate.sh: WARNING: $end_warning" >&2
}
on_exit() {
    local rc=$?
    trap - EXIT
    finish
    warn_end
    exit "$rc"
}
trap on_exit EXIT
# INT and HUP cleanup apply only when the signal was not ignored at launch: bash cannot
# trap a signal that was ignored on entry. A backgrounded launch (cmd & from a
# non-interactive shell) inherits SIGINT=SIG_IGN and a nohup launch inherits
# SIGHUP=SIG_IGN, so that signal then does nothing here; TERM always runs the cleanup
# (nexus-frfeh, nexus-ys8a4).
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [[ -n "$wheel_name" ]]; then
    say "candidate wheel $wheel_name sha256 $wheel_sha (not a published build)"
else
    say "conexus==$version from PyPI"
fi
git_note="Git is installed for all users by $GUSER"
if [[ "$admin_git" == no ]]; then git_note="no Git from $GUSER: the account installs PortableGit with the block in docs/windows-install.md"; fi
if [[ "$applocker" == yes ]]; then git_note="$git_note; AppLocker default rules enforced"; fi
if [[ "$applocker" == allow ]]; then git_note="$git_note; AppLocker default rules enforced for programs, with allow rules for this install's folders; script rules audit only"; fi
if [[ "$applocker" == audit ]]; then git_note="$git_note; AppLocker default rules in audit only (nothing is blocked)"; fi
if [[ "$applocker" == allow-dll-audit ]]; then git_note="$git_note; AppLocker default rules enforced for programs, with allow rules for this install's folders; DLL rules and script rules audit only"; fi
if [[ "$applocker" == allow-dll ]]; then git_note="$git_note; AppLocker default rules enforced for programs and DLLs, with allow rules for this install's folders; script rules audit only"; fi
if [[ "$wdac" == audit ]]; then git_note="$git_note; WDAC DefaultWindows policy in audit mode (nothing is blocked)"; fi
if [[ "$wdac" == allow ]]; then git_note="$git_note; WDAC DefaultWindows policy enforced, with path rules for this install's folders; script enforcement off"; fi
if [[ $standard_user -eq 1 ]]; then say "standard user $STD_USER (not an administrator); $git_note"; fi
# What a resumed run no longer proves (nexus-y3y8d), said at the start and again in the summary.
signed_in_limit=''
if [[ -n "$signed_in" ]]; then
    signed_in_limit="resumed from checkpoint $signed_in, not restored to $CHECKPOINT: this run does not exercise $STD_USER's first sign-in or the boot after a restore, so it is a different claim from a run restored to the baseline and must not be cited as one"
    say "$signed_in_limit"
fi
if [[ -n "$signed_in_build" ]]; then
    say "building the one running checkpoint $signed_in_build as a child of $CHECKPOINT on $VM: its memory image holds $STD_USER's signed-in session; the account secret is removed from Winlogon before the checkpoint; no gate is run"
fi
say "engine $engine, gate script sha256 $gate_sha, run $RUN, logs in $out"

# --- stage ------------------------------------------------------------------
# mkdir fails when the directory exists, so a collision stops here.
run_bounded 120 ssh "${SSH_OPTS[@]}" "$HOST" "cmd /c \"mkdir $HOST_WIN_DIR\\stage\"" </dev/null 2>&1 | quiet
[[ ${PIPESTATUS[0]} -eq 0 ]] || die 1 "cannot create $HOST_DIR on $HOST"
host_dir_made=1
scp "${SSH_OPTS[@]}" -q "$driver"/*.ps1 "$HOST:$HOST_DIR/" 2>&1 | quiet
[[ ${PIPESTATUS[0]} -eq 0 ]] || die 1 "cannot copy the driver scripts to $HOST:$HOST_DIR"
if [[ "$admin_git" == no ]]; then
    scp "${SSH_OPTS[@]}" -q "$out/portable-git.ps1" "$HOST:$HOST_DIR/" 2>&1 | quiet
    [[ ${PIPESTATUS[0]} -eq 0 ]] || die 1 "cannot copy the PortableGit block to $HOST:$HOST_DIR"
fi
if [[ -n "$wheel_name" ]]; then
    scp "${SSH_OPTS[@]}" -q "$gate" "$wheel" "$HOST:$HOST_DIR/stage/" 2>&1 | quiet
else
    scp "${SSH_OPTS[@]}" -q "$gate" "$HOST:$HOST_DIR/stage/" 2>&1 | quiet
fi
[[ ${PIPESTATUS[0]} -eq 0 ]] || die 1 "cannot copy the gate script to $HOST:$HOST_DIR/stage"

# --- run --------------------------------------------------------------------
# --signed-in: the guest resumes from the signed-in checkpoint (hostcheck has checked it), --build-signed-in and every
# other run restore the baseline checkpoint.
hostcheck_extra=''
restore_checkpoint="$CHECKPOINT"
restore_resume=''
if [[ -n "$signed_in" ]]; then
    hostcheck_extra="-SignedIn $signed_in"
    restore_checkpoint="$signed_in"
    restore_resume='-Resume yes'
fi
if [[ -n "$signed_in_build" ]]; then
    hostcheck_extra="-NewCheckpoint $signed_in_build"
    # The build runs no gate and collects nothing: the on-exit collection (which opens the guest with the
    # keychain secret) has nothing to fetch, whatever step the run stops at.
    collected=1
fi
# shellcheck disable=SC2086
run_step 00 hostcheck "$T_HOSTCHECK" 1 host_ps hostcheck.ps1 -VMName "$VM" -Checkpoint "$CHECKPOINT" -RunDir "$HOST_WIN_DIR" $hostcheck_extra

# shellcheck disable=SC2086
if [[ -n "$wheel_name" ]]; then
    run_step 01 restore "$T_RESTORE" 1 host_ps_pw restore.ps1 -VMName "$VM" -GuestUser "$GUSER" -Checkpoint "$restore_checkpoint" -RunDir "$HOST_WIN_DIR" -Wheel "$wheel_name" -Cache "$restore_cache" $restore_resume
    grep -qF "wheel sha256=$wheel_sha" "$out/01-restore.txt" || die 1 "the wheel in the guest does not have sha256 $wheel_sha"
else
    run_step 01 restore "$T_RESTORE" 1 host_ps_pw restore.ps1 -VMName "$VM" -GuestUser "$GUSER" -Checkpoint "$restore_checkpoint" -RunDir "$HOST_WIN_DIR" -Cache "$restore_cache" $restore_resume
fi
grep -qF "gate sha256=$gate_sha" "$out/01-restore.txt" || die 1 "the gate script in the guest does not have sha256 $gate_sha"

if [[ $standard_user -eq 1 ]]; then
    wheel_arg=''
    if [[ -n "$wheel_name" ]]; then wheel_arg="-Wheel $wheel_name"; fi
    existing_arg=''
    if [[ -n "$signed_in" ]]; then existing_arg='-Existing yes'; fi
    # shellcheck disable=SC2086
    run_step 01s stduser "$T_STDUSER" 1 host_ps_pw stduser.ps1 -VMName "$VM" -GuestUser "$GUSER" -StdUser "$STD_USER" -RunDir "$HOST_WIN_DIR" -AdminGit "$admin_git" -AppLocker "$applocker" -Wdac "$wdac" $wheel_arg $existing_arg
    grep -qF "std gate sha256=$gate_sha" "$out/01s-stduser.txt" || die 1 "the gate script in $STD_USER's profile does not have sha256 $gate_sha"
    if [[ -n "$wheel_name" ]]; then
        grep -qF "std wheel sha256=$wheel_sha" "$out/01s-stduser.txt" || die 1 "the wheel in $STD_USER's profile does not have sha256 $wheel_sha"
    fi
    # From here every step works in the standard user's profile, and the install runs as that account.
    home_user="$STD_USER"
    run_as="-RunAs $STD_USER"
fi

# --build-signed-in (nexus-y3y8d): take the one running checkpoint of the signed-in guest, then the normal end step. The
# gate is not run, so there is no verdict and nothing to collect from the guest. The stduser step must have removed the
# account secret from Winlogon before the guest's memory is saved.
if [[ -n "$signed_in_build" ]]; then
    grep -qF 'winlogon secret removed=True' "$out/01s-stduser.txt" || die 1 "the stduser step did not print 'winlogon secret removed=True'; no checkpoint is taken; log: $out/01s-stduser.txt"
    # shellcheck disable=SC2153
    run_step 01k checkpoint "$T_CHECKPOINT" 1 host_ps checkpoint.ps1 -VMName "$VM" -Baseline "$CHECKPOINT" -Name "$signed_in_build" -RunDir "$HOST_WIN_DIR" -SettleSeconds "$SETTLE_S"
    finish
    [[ -z "$end_warning" ]] || die 1 "the checkpoint $signed_in_build was taken, but the end step did not finish clean (the guest's CheckpointType may not be back as it was): $end_warning"
    echo "run-gate.sh: SIGNED-IN CHECKPOINT BUILT (no gate was run; this is not a gate verdict): the running checkpoint $signed_in_build is a child of the baseline checkpoint $CHECKPOINT on $VM (settled $SETTLE_S s); its memory image holds $STD_USER's signed-in session and the account secret was removed from Winlogon before the checkpoint; the guest is shut down; logs in $out"
    exit 0
fi
as_user="${run_as:+-AsUser yes}"

# --cache on (nexus-bpzt7): the installing account's uv cache and a Claude Code binary come from the host's
# C:\build\guest\cache (cache.ps1 seed), and step 07 refills it after the run.
if [[ "$cache" == on ]]; then
    run_step 01c seed "$T_CACHE" 1 host_ps_pw cache.ps1 -VMName "$VM" -GuestUser "$GUSER" -Mode seed -Account "$home_user" -Key "$cache_key"
fi

# shellcheck disable=SC2086
if [[ -n "$wheel_name" ]]; then
    run_step 02 install "$T_INSTALL" 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner inst.ps1 -Arg1 "$version" -Arg2 "$wheel_name" $run_as $as_user
else
    run_step 02 install "$T_INSTALL" 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner inst.ps1 -Arg1 "$version" $run_as $as_user
fi
# shellcheck disable=SC2086
run_step 03 setup "$T_SETUP" 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner setup-run.ps1 -Arg1 "$version" -Arg2 "$engine" $run_as
# shellcheck disable=SC2086
if [[ -n "$wheel_name" ]]; then
    run_step 04 gaterun "$T_GATERUN" 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner gaterun.ps1 -Arg1 "$version" -Arg2 "$engine" -Arg3 candidate $run_as
else
    run_step 04 gaterun "$T_GATERUN" 1 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner gaterun.ps1 -Arg1 "$version" -Arg2 "$engine" $run_as
fi
run_step 05 launch "$T_LAUNCH" 0 launch_guest
# shellcheck disable=SC2086
run_step 06 verdict "$T_VERDICT" 0 host_ps_pw guest-run.ps1 -VMName "$VM" -GuestUser "$GUSER" -RunDir "$HOST_WIN_DIR" -Inner waitv.ps1 -Collect yes $run_as

fetch_collected
[[ $collected -eq 1 ]] || die 1 "the guest's run directory was not collected"

# Refill the host cache from this guest. Not part of the verdict: a failure here is reported and the run goes on.
if [[ "$cache" == on ]]; then
    say "step 07 harvest"
    run_bounded "$T_CACHE" host_ps_pw cache.ps1 -VMName "$VM" -GuestUser "$GUSER" -Mode harvest -Account "$home_user" -Key "$cache_key" 2>&1 | quiet | tee "$out/07-harvest.txt"
    grep -q '^NXG-RESULT: ok' "$out/07-harvest.txt" || say "the cache harvest did not finish (not part of the verdict); log: $out/07-harvest.txt"
fi

# --- verdict ----------------------------------------------------------------
verdict_file="$out/run/verdict.txt"
[[ -f "$verdict_file" ]] || die 1 "no verdict.txt was collected: the run has no verdict, which is a FAILED run"
# The file the gate writes on Windows is UTF-8 with a BOM on line 1 and CRLF line ends.
norm="$(LC_ALL=C sed $'s/^\xef\xbb\xbf//' "$verdict_file" | tr -d '\r')"
verdict="$(printf '%s\n' "$norm" | grep -v '^[[:space:]]*$' | tail -n 1)"
passed_re='^WINDOWS PHASE 5 GATE PASSED \(executed=([0-9]+) declared=([0-9]+)\)$'
sha_re="^gate script sha256 ${gate_sha} \\(.*; the setup phase ran the same file\\)\$"
rc=1
provenance_ok=1
if [[ "$verdict" =~ $passed_re ]] && [[ "${BASH_REMATCH[1]}" == "${BASH_REMATCH[2]}" ]] && [[ "${BASH_REMATCH[1]}" -gt 0 ]]; then
    # A PASSED line from some other file (a verify run of a different script) is not this run's verdict.
    if grep -qE "$sha_re" <<<"$norm"; then rc=0; else provenance_ok=0; fi
fi
finish
echo "$verdict"
if [[ $provenance_ok -eq 0 ]]; then
    echo "run-gate.sh: the verdict is PASSED but verdict.txt does not carry the gate script line for this run's gate script (gate script sha256 $gate_sha (...; the setup phase ran the same file)); not accepted" >&2
fi
if [[ -n "$wheel_name" ]]; then
    echo "run-gate.sh: candidate wheel $wheel_name (sha256 $wheel_sha), not a published build; $cache_note; gate script sha256 $gate_sha; logs in $out"
else
    echo "run-gate.sh: conexus==$version from PyPI; $cache_note; gate script sha256 $gate_sha; logs in $out"
fi
if [[ $standard_user -eq 1 ]]; then echo "run-gate.sh: run as standard user $STD_USER (not an administrator); $git_note"; fi
if [[ -n "$signed_in_limit" ]]; then echo "run-gate.sh: NOTE $signed_in_limit"; fi
exit "$rc"
