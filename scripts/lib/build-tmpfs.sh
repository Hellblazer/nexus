#!/usr/bin/env bash
# scripts/lib/build-tmpfs.sh -- where a gate-jar build's Maven build directory
# lives (bead nexus-0a1hw). Sourced, never executed. Used by
# scripts/build-gate-jar.sh.
#
# WHY. A gate-jar MISS ran `mvnw package` into service/target and, on the chas
# lab host, wrote 316 MiB to the DRAM-less NVMe (device delta with a 35 s
# writeback tail; develop 620cdc02d, jOOQ cache HIT, quiet pool). On a laptop the
# build processes wrote ~245 MiB: the shaded jar ~131 MB, the jlink image ~83 MB,
# and ~15-35 MiB of classes, generated sources and maven-status. On a lab host the
# build directory goes to a per-build directory on tmpfs (service/pom.xml's
# nexus.build.dir, default ${project.basedir}/target) and only the stamped jar
# reaches the disk, once per key, as the cache entry (scripts/lib/gate-jar-cache.sh).
#
# WHAT IT SAVES, AND WHAT STAYS. The jlink skip (-Dnexus.jlink.skip=true, every
# host, ~83 MiB) is the larger saving; moving the build directory saves only the
# Maven output other than the jar (~15-35 MiB). The stamped jar (~131 MB) is still
# written once per key and is the floor. The jOOQ codegen on a jOOQ cache miss
# (~350 MB in the container overlay, no build-directory knob) is untouched, which
# is why the auto decision below stays on disk when the cache cannot serve the
# build. The chas figure after the change comes from the chas proof run (bead
# nexus-0a1hw), not from this file.
#
# WHEN. NX_BUILD_TMPFS:
#   unset  -> auto: on Linux, inside a lab-run grant (LAB_CPUS set), with a usable
#             /dev/shm, a readable grant memory limit, and a jOOQ cache that can
#             serve the build (below). Anywhere else the build stays in
#             service/target exactly as before (macOS, a laptop, an IDE, a bare
#             mvn, CI jobs that run mvn themselves).
#   off|0  -> never.
#   on|1   -> try on any Linux box with a usable /dev/shm (outside a grant too).
#   /dir   -> try under that directory (any OS); the test seam, and an escape for
#             a host whose tmpfs is mounted elsewhere.
# An explicit on or /dir skips the refusals that are about the jOOQ cache and an
# unreadable grant limit, and reports each as a warning (BUILD_TMPFS_WARN)
# instead. Free space, headroom, a bad BUILD_TMPFS_MIN_MIB and an unsafe per-user
# directory refuse in every mode.
#
# FAIL SAFE TO DISK. Every uncertainty is a refusal, never a guess:
#   - inside a grant, a memory limit that cannot be read (no cgroup-v2 memory.max
#     on the path) refuses: tmpfs pages are charged to the grant, so the bound
#     cannot be checked;
#   - a BUILD_TMPFS_MIN_MIB that is not a whole number refuses;
#   - the jOOQ cache gate: a fresh build directory carries no jOOQ stamp, so the
#     codegen is skipped only by restoring a tree from the shared cache. The auto
#     decision therefore needs the cache directory (as the pom resolves it:
#     -Dnexus.jooq.cache.dir, then NX_JOOQ_CACHE, then <git common dir>/
#     nexus-jooq-cache) to be on, writable, and to hold a complete main-* and
#     test-* entry. The pom's input hash is not reproduced here, so this is a
#     floor, not proof: an entry for other inputs passes it and the codegen then
#     runs once. build-gate-jar.sh says so when a tmpfs build published a new entry;
#   - -Dnexus.jooq.regenerate=true forces the codegen and refuses too;
#   - the per-user directory is refused when it is a symlink or not owned by us.
# build-gate-jar.sh adds one more step: a tmpfs build that exits 137 or reports
# "No space left" is retried once on disk.
#
# BOUNDS. tmpfs pages are charged to the lab-run grant's cgroup, so a build that
# does not fit is killed (exit 137) instead of slow. A tmpfs build directory is
# about 170 MiB (jar ~131 MB, classes, generated sources; the jlink image is
# skipped). The decision needs BUILD_TMPFS_MIN_MIB (default 1024, a margin, not a
# measured footprint) of free tmpfs space AND of cgroup-v2 memory headroom (page
# cache the kernel can drop is credited, as tests/_engine_substrate.py does for its
# Postgres clusters); anything less builds on disk.
#
# CLEANUP. The directory is removed on every exit path of the caller (call
# build_tmpfs_cleanup from its EXIT trap), after the build's own process group is
# stopped: a plain SIGTERM to the script otherwise leaves Maven running, and Maven
# recreates the directory after the trap removed it. build_tmpfs_run starts the
# build in its own process group for that. A SIGKILLed script cannot clean up, and
# /dev/shm is RAM, so the directory's name carries the owner's pid and the next
# prepare removes directories of dead pids older than BUILD_TMPFS_STALE_MIN
# (default 120). Directories live under <base>/nexus-build-<uid>/, mode 0700.
#
# The cache stays on disk. Moving it to tmpfs would make every HIT copy 131 MB
# across filesystems (a hard link cannot cross them) instead of linking it, and
# lose the entries on reboot; the link on a HIT is what makes a HIT free.

set -u -o pipefail

BUILD_TMPFS_MIN_MIB="${BUILD_TMPFS_MIN_MIB:-1024}"
BUILD_TMPFS_STALE_MIN="${BUILD_TMPFS_STALE_MIN:-120}"

# Results of the last build_tmpfs_prepare: the directory ("" = build on disk), a
# one-line reason for whichever way it went, warnings an explicit mode chose to go
# past (";"-joined, "" = none), and the jOOQ cache root the build will use ("off"
# or "" when none).
BUILD_TMPFS_DIR=""
BUILD_TMPFS_WHY=""
BUILD_TMPFS_WARN=""
BUILD_TMPFS_JOOQ_ROOT=""
# The pid (= process group id) of the build build_tmpfs_run is waiting on.
BUILD_TMPFS_CHILD_PID=""

# _build_tmpfs_cgroup_headroom_mib -- MiB of memory headroom under the tightest
# cgroup-v2 limit on this process's path (own and ancestors), page cache credited;
# prints nothing when there is no limit or no v2 hierarchy. Seams for tests:
# BUILD_TMPFS_PROC_CGROUP (contents of /proc/self/cgroup, or a file), and
# BUILD_TMPFS_CGROUP_ROOT (default /sys/fs/cgroup).
_build_tmpfs_cgroup_headroom_mib() {
    local root="${BUILD_TMPFS_CGROUP_ROOT:-/sys/fs/cgroup}" text rel d best="" mx cur room rec p
    if [[ -n "${BUILD_TMPFS_PROC_CGROUP:-}" ]]; then
        text="$BUILD_TMPFS_PROC_CGROUP"
        [[ -f "$text" ]] && text="$(cat "$text")"
    else
        text="$(cat /proc/self/cgroup 2>/dev/null)" || return 0
    fi
    rel="$(printf '%s\n' "$text" | sed -n 's/^0:://p' | head -1)"
    [[ -n "$rel" ]] || return 0
    local -a parts=()
    local IFS=/
    # shellcheck disable=SC2206
    parts=($rel)
    IFS=$' \t\n'
    local depth=${#parts[@]} i
    for ((i = depth; i >= 0; i--)); do
        d="$root"
        for ((p = 0; p < i; p++)); do d="$d/${parts[p]}"; done
        mx="$(cat "$d/memory.max" 2>/dev/null)" || continue
        cur="$(cat "$d/memory.current" 2>/dev/null)" || continue
        [[ "$mx" =~ ^[0-9]+$ && "$cur" =~ ^[0-9]+$ ]] || continue
        rec="$(awk '$1=="active_file"||$1=="inactive_file"{s+=$2} END{print s+0}' "$d/memory.stat" 2>/dev/null)"
        [[ "$rec" =~ ^[0-9]+$ ]] || rec=0
        room=$(( (mx - cur + rec) / 1048576 ))
        if [[ -z "$best" ]] || (( room < best )); then best="$room"; fi
    done
    [[ -z "$best" ]] || printf '%s\n' "$best"
}

# _build_tmpfs_free_mib <dir> -- free MiB on the filesystem holding <dir>.
_build_tmpfs_free_mib() {
    df -Pk "$1" 2>/dev/null | awk 'NR==2{print int($4/1024)}'
}

# _build_tmpfs_jooq_root <repo> [mvn args...] -- the jOOQ cache directory service/pom.xml
# will use, by the pom's own order: -Dnexus.jooq.cache.dir (last one wins), then
# $NX_JOOQ_CACHE, then <git common dir>/nexus-jooq-cache. Prints "off" when the
# cache is off (the value off/0/empty, or no git checkout).
_build_tmpfs_jooq_root() {
    local repo="$1" a v="" seen=0 common
    shift
    for a in "$@"; do
        case "$a" in -Dnexus.jooq.cache.dir=*) v="${a#*=}"; seen=1 ;; esac
    done
    if (( ! seen )); then
        v="${NX_JOOQ_CACHE:-}"
        if [[ -z "$v" ]]; then
            if common="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" && [[ -n "$common" ]]; then
                v="$common/nexus-jooq-cache"
            fi
        fi
    fi
    case "$v" in off|OFF|Off|0|"") printf 'off\n' ;; *) printf '%s\n' "$v" ;; esac
}

# _build_tmpfs_jooq_has_entry <root> <main|test> -- a complete entry of that kind: a
# non-empty `complete` marker and at least one generated .java file.
_build_tmpfs_jooq_has_entry() {
    local d
    for d in "$1/$2"-*; do
        [[ -d "$d" && "$d" != *.old* && -s "$d/complete" ]] || continue
        [[ -n "$(find "$d/generated" -name '*.java' -print -quit 2>/dev/null)" ]] && return 0
    done
    return 1
}

# build_tmpfs_jooq_entries <root> -- the names of the main-*/test-* entries, sorted;
# the caller diffs this before and after a build to see that the codegen ran.
build_tmpfs_jooq_entries() {
    [[ -d "${1:-}" ]] || return 0
    find "$1" -mindepth 1 -maxdepth 1 -type d \( -name 'main-*' -o -name 'test-*' \) ! -name '*.old*' 2>/dev/null | sed 's|.*/||' | sort
}

# _build_tmpfs_jooq_problem [mvn args...] -- prints why the jOOQ cache cannot be
# expected to serve a build in a fresh directory (BUILD_TMPFS_JOOQ_ROOT is set);
# prints nothing when it can.
_build_tmpfs_jooq_problem() {
    local a root="$BUILD_TMPFS_JOOQ_ROOT"
    for a in "$@"; do
        if [[ "$a" == -Dnexus.jooq.regenerate=true ]]; then
            echo "-Dnexus.jooq.regenerate=true forces the jOOQ codegen"; return 0
        fi
    done
    if [[ "$root" == off || -z "$root" ]]; then
        echo "the jOOQ cache is off (NX_JOOQ_CACHE or -Dnexus.jooq.cache.dir): a fresh build directory would re-run the jOOQ codegen container"
        return 0
    fi
    if [[ ! -d "$root" || ! -w "$root" ]]; then
        echo "the jOOQ cache $root is missing or not writable: a fresh build directory would re-run the jOOQ codegen container"
        return 0
    fi
    if ! _build_tmpfs_jooq_has_entry "$root" main || ! _build_tmpfs_jooq_has_entry "$root" test; then
        echo "the jOOQ cache $root holds no complete main-* and test-* entry: a fresh build directory would re-run the jOOQ codegen container"
        return 0
    fi
}

# _build_tmpfs_sweep <userdir> -- remove build directories whose owner pid is
# dead and that are older than BUILD_TMPFS_STALE_MIN (see CLEANUP above).
_build_tmpfs_sweep() {
    local userdir="$1" d name pid
    [[ -d "$userdir" ]] || return 0
    while IFS= read -r d; do
        [[ -n "$d" ]] || continue
        name="${d##*/}"
        pid="${name#b.}"; pid="${pid%%.*}"
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        kill -0 "$pid" 2>/dev/null && continue
        rm -rf "${d:?}"
    done < <(find "$userdir" -mindepth 1 -maxdepth 1 -type d -name 'b.*' -mmin "+$BUILD_TMPFS_STALE_MIN" 2>/dev/null)
}

# _build_tmpfs_owner <path> -- the numeric owner of <path> (GNU stat, then BSD).
_build_tmpfs_owner() {
    stat -c %u "$1" 2>/dev/null || stat -f %u "$1" 2>/dev/null
}

# build_tmpfs_prepare [<repo> [mvn args...]] -- decide (see WHEN/FAIL SAFE/BOUNDS)
# and, when tmpfs is wanted and fits, create the per-build directory. <repo> and
# the mvn args (the ones the build will get) feed the jOOQ cache gate. Sets
# BUILD_TMPFS_DIR (the directory, or "" to build in service/target),
# BUILD_TMPFS_WHY, BUILD_TMPFS_WARN and BUILD_TMPFS_JOOQ_ROOT. Always returns 0:
# this is an optimisation, and every refusal is a plain disk build.
# shellcheck disable=SC2034  # the results are read by the sourcing script
build_tmpfs_prepare() {
    BUILD_TMPFS_DIR=""
    BUILD_TMPFS_WHY=""
    BUILD_TMPFS_WARN=""
    BUILD_TMPFS_JOOQ_ROOT=""
    local repo="${1:-.}"
    [[ $# -eq 0 ]] || shift
    local mode="${NX_BUILD_TMPFS:-}" base os free head userdir dir uid owner problem explicit=0
    os="${BUILD_TMPFS_OS:-$(uname -s 2>/dev/null)}"
    case "$mode" in
        off|OFF|0|false) BUILD_TMPFS_WHY="NX_BUILD_TMPFS=off"; return 0 ;;
        "")
            if [[ "$os" != Linux ]]; then
                BUILD_TMPFS_WHY="not Linux ($os)"; return 0
            fi
            if [[ -z "${LAB_CPUS:-}" ]]; then
                BUILD_TMPFS_WHY="not inside a lab-run grant (LAB_CPUS unset); NX_BUILD_TMPFS=on forces it"; return 0
            fi
            base="${BUILD_TMPFS_SHM:-/dev/shm}" ;;
        on|ON|1|true)
            if [[ "$os" != Linux ]]; then
                BUILD_TMPFS_WHY="NX_BUILD_TMPFS=on but not Linux ($os)"; return 0
            fi
            explicit=1
            base="${BUILD_TMPFS_SHM:-/dev/shm}" ;;
        /*) explicit=1; base="$mode" ;;
        *) BUILD_TMPFS_WHY="NX_BUILD_TMPFS=$mode not understood (off, on or an absolute directory)"; return 0 ;;
    esac
    if [[ ! "$BUILD_TMPFS_MIN_MIB" =~ ^[0-9]+$ ]]; then
        BUILD_TMPFS_WHY="BUILD_TMPFS_MIN_MIB='$BUILD_TMPFS_MIN_MIB' is not a whole number of MiB; building on disk"; return 0
    fi
    # The jOOQ cache gate (see FAIL SAFE TO DISK).
    BUILD_TMPFS_JOOQ_ROOT="$(_build_tmpfs_jooq_root "$repo" "$@")"
    problem="$(_build_tmpfs_jooq_problem "$@")"
    if [[ -n "$problem" ]]; then
        if (( explicit )); then BUILD_TMPFS_WARN="$problem"; else BUILD_TMPFS_WHY="$problem"; return 0; fi
    fi
    if [[ ! -d "$base" || ! -w "$base" ]]; then
        BUILD_TMPFS_WHY="$base is not a writable directory"; return 0
    fi
    free="$(_build_tmpfs_free_mib "$base")"
    if [[ ! "$free" =~ ^[0-9]+$ ]] || (( free < BUILD_TMPFS_MIN_MIB )); then
        BUILD_TMPFS_WHY="$base has ${free:-unknown} MiB free, under the ${BUILD_TMPFS_MIN_MIB} MiB a build needs"; return 0
    fi
    head="$(_build_tmpfs_cgroup_headroom_mib)"
    if [[ -n "$head" ]]; then
        if (( head < BUILD_TMPFS_MIN_MIB )); then
            BUILD_TMPFS_WHY="the grant has ${head} MiB of memory headroom, under the ${BUILD_TMPFS_MIN_MIB} MiB a tmpfs build needs"; return 0
        fi
    elif [[ -n "${LAB_CPUS:-}" ]]; then
        problem="inside a lab-run grant but its memory limit cannot be read (no cgroup-v2 memory.max on this process's path): tmpfs pages are charged to the grant, so the bound cannot be checked"
        if (( explicit )); then
            BUILD_TMPFS_WARN="${BUILD_TMPFS_WARN:+$BUILD_TMPFS_WARN; }$problem"
        else
            BUILD_TMPFS_WHY="$problem"; return 0
        fi
    fi
    uid="${BUILD_TMPFS_UID:-$(id -u 2>/dev/null || echo u)}"
    userdir="$base/nexus-build-$uid"
    if [[ -L "$userdir" ]]; then
        BUILD_TMPFS_WHY="$userdir is a symlink; refusing to build under it"; return 0
    fi
    if ! mkdir -p "$userdir" 2>/dev/null; then
        BUILD_TMPFS_WHY="cannot create $userdir"; return 0
    fi
    owner="$(_build_tmpfs_owner "$userdir")"
    if [[ -L "$userdir" || ! -d "$userdir" || "$owner" != "$uid" ]]; then
        BUILD_TMPFS_WHY="$userdir is not a directory owned by uid $uid (owner ${owner:-unknown}); refusing to build under it"; return 0
    fi
    if ! chmod 700 "$userdir" 2>/dev/null; then
        BUILD_TMPFS_WHY="cannot create $userdir"; return 0
    fi
    _build_tmpfs_sweep "$userdir"
    if ! dir="$(mktemp -d "$userdir/b.$$.XXXXXX" 2>/dev/null)" || [[ -z "$dir" ]]; then
        BUILD_TMPFS_WHY="cannot create a build directory under $userdir"; return 0
    fi
    BUILD_TMPFS_DIR="$dir"
    BUILD_TMPFS_WHY="${free} MiB free${head:+, ${head} MiB grant headroom}"
    return 0
}

# build_tmpfs_run <log|""> <command...> -- run <command> in its OWN process group
# (job control for the one fork) so build_tmpfs_stop_child can stop all of it, and
# return its exit code. With a <log> the command's stdout and stderr go to that file
# and are printed when it ends (so the caller can read them); without one they
# stay on the caller's. stdin is /dev/null.
build_tmpfs_run() {
    local log="$1" rc=0
    shift
    BUILD_TMPFS_CHILD_PID=""
    set -m
    if [[ -n "$log" ]]; then
        "$@" </dev/null >"$log" 2>&1 &
    else
        "$@" </dev/null &
    fi
    BUILD_TMPFS_CHILD_PID=$!
    set +m
    wait "$BUILD_TMPFS_CHILD_PID" || rc=$?
    BUILD_TMPFS_CHILD_PID=""
    if [[ -n "$log" ]]; then cat "$log" 2>/dev/null || true; fi
    return "$rc"
}

# build_tmpfs_stop_child -- stop the build build_tmpfs_run started, if it is still
# running: TERM to its process group, up to 5 s for the leader, up to 3 s for the
# rest of the group, then KILL. Safe to call when nothing runs.
build_tmpfs_stop_child() {
    local pid="${BUILD_TMPFS_CHILD_PID:-}" i state
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    BUILD_TMPFS_CHILD_PID=""
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for ((i = 0; i < 50; i++)); do
        # A pipeline that fails (ps finds no such pid) must not end a caller that runs under set -e.
        state="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')" || state=""
        [[ -z "$state" || "$state" == Z* ]] && break
        sleep 0.1
    done
    # A leader that outlived TERM (it ignores it, or a slow shutdown) is killed before
    # the wait, or the wait would block until it exits on its own (nexus-0a1hw review).
    if [[ -n "$state" && "$state" != Z* ]]; then
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
    for ((i = 0; i < 30; i++)); do
        kill -0 -- "-$pid" 2>/dev/null || break
        sleep 0.1
    done
    # Only a group that still exists is killed, so a reused pid is never signalled.
    if kill -0 -- "-$pid" 2>/dev/null; then
        kill -KILL -- "-$pid" 2>/dev/null || true
    fi
    return 0
}

# build_tmpfs_cleanup -- stop a build still running, then remove the directory
# build_tmpfs_prepare made. Safe to call twice and from an EXIT trap.
build_tmpfs_cleanup() {
    build_tmpfs_stop_child
    if [[ -n "${BUILD_TMPFS_DIR:-}" && "$BUILD_TMPFS_DIR" == */b.* ]]; then
        rm -rf "${BUILD_TMPFS_DIR:?}" 2>/dev/null || true
    fi
    BUILD_TMPFS_DIR=""
    return 0
}
