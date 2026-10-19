#!/usr/bin/env bash
# scripts/lib/build-tmpfs.sh -- where a gate-jar build's Maven build directory
# lives (bead nexus-0a1hw). Sourced, never executed. Used by
# scripts/build-gate-jar.sh.
#
# WHY. A gate-jar MISS ran `mvnw package` into service/target and wrote ~0.5 GB
# to the lab hosts' DRAM-less NVMe (classes, the thin jar, the 131 MB shaded jar,
# the 83 MB jlink image, the jOOQ trees, maven-status), on every service/src/main
# change, in every worktree and every CI job. None of it is read again except the
# one stamped jar, which the gate-jar cache already holds. So on a lab host the
# build directory goes to a per-build directory on tmpfs (service/pom.xml's
# nexus.build.dir, default ${project.basedir}/target), and only the stamped jar
# reaches the disk, once per key, as the cache entry (scripts/lib/gate-jar-cache.sh).
#
# WHEN. NX_BUILD_TMPFS:
#   unset  -> auto: on Linux, inside a lab-run grant (LAB_CPUS set), with a usable
#             /dev/shm and the jOOQ cache not switched off. Anywhere else the build
#             stays in service/target exactly as before (macOS, a laptop, an IDE,
#             a bare mvn, CI jobs that run mvn themselves).
#   off|0  -> never.
#   on|1   -> try on any Linux box with a usable /dev/shm (outside a grant too).
#   /dir   -> try under that directory (any OS); the test seam, and an escape for
#             a host whose tmpfs is mounted elsewhere.
# The jOOQ cache gate: a fresh build directory carries no jOOQ stamp, so with
# NX_JOOQ_CACHE=off every build would boot the codegen container (~350 MB written
# inside it), far worse than the 0.5 GB this saves. Auto therefore stays on disk
# there, where the stamp in service/target survives between builds.
#
# BOUNDS. tmpfs pages are charged to the lab-run grant's cgroup, so a build that
# does not fit is killed (exit 137) instead of slow. A build directory is ~300 MB
# (jar 131 MB, jlink image 83 MB, classes, generated sources). The decision needs
# BUILD_TMPFS_MIN_MIB (default 1024) of free tmpfs space AND of cgroup-v2 memory
# headroom (page cache the kernel can drop is credited, as tests/_engine_substrate.py
# does for its Postgres clusters); anything less builds on disk.
#
# CLEANUP. The directory is removed on every exit path of the caller (call
# build_tmpfs_cleanup from its EXIT trap). A SIGKILLed build cannot, and /dev/shm
# is RAM, so the directory's name carries the owner's pid and the next prepare
# removes directories of dead pids older than BUILD_TMPFS_STALE_MIN (default 120).
# Directories live under <base>/nexus-build-<uid>/, mode 0700.
#
# The cache stays on disk. Moving it to tmpfs would make every HIT copy 131 MB
# across filesystems (a hard link cannot cross them) instead of linking it, and
# lose the entries on reboot; the link on a HIT is what makes a HIT free.

set -u -o pipefail

BUILD_TMPFS_MIN_MIB="${BUILD_TMPFS_MIN_MIB:-1024}"
BUILD_TMPFS_STALE_MIN="${BUILD_TMPFS_STALE_MIN:-120}"

# Results of the last build_tmpfs_prepare: the directory ("" = build on disk) and
# a one-line reason for whichever way it went.
BUILD_TMPFS_DIR=""
BUILD_TMPFS_WHY=""

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

# build_tmpfs_prepare -- decide (see WHEN/BOUNDS) and, when tmpfs is wanted and
# fits, create the per-build directory. Sets BUILD_TMPFS_DIR (the directory, or ""
# to build in service/target) and BUILD_TMPFS_WHY. Always returns 0: this is an
# optimisation, and every refusal is a plain disk build.
# shellcheck disable=SC2034  # BUILD_TMPFS_WHY is read by the sourcing script
build_tmpfs_prepare() {
    BUILD_TMPFS_DIR=""
    BUILD_TMPFS_WHY=""
    local mode="${NX_BUILD_TMPFS:-}" base os free head userdir dir
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
            case "${NX_JOOQ_CACHE:-}" in
                off|OFF|0) BUILD_TMPFS_WHY="NX_JOOQ_CACHE=off: a fresh build directory would re-run the jOOQ codegen container"; return 0 ;;
            esac
            base="${BUILD_TMPFS_SHM:-/dev/shm}" ;;
        on|ON|1|true)
            if [[ "$os" != Linux ]]; then
                BUILD_TMPFS_WHY="NX_BUILD_TMPFS=on but not Linux ($os)"; return 0
            fi
            base="${BUILD_TMPFS_SHM:-/dev/shm}" ;;
        /*) base="$mode" ;;
        *) BUILD_TMPFS_WHY="NX_BUILD_TMPFS=$mode not understood (off, on or an absolute directory)"; return 0 ;;
    esac
    if [[ ! -d "$base" || ! -w "$base" ]]; then
        BUILD_TMPFS_WHY="$base is not a writable directory"; return 0
    fi
    free="$(_build_tmpfs_free_mib "$base")"
    if [[ ! "$free" =~ ^[0-9]+$ ]] || (( free < BUILD_TMPFS_MIN_MIB )); then
        BUILD_TMPFS_WHY="$base has ${free:-unknown} MiB free, under the ${BUILD_TMPFS_MIN_MIB} MiB a build needs"; return 0
    fi
    head="$(_build_tmpfs_cgroup_headroom_mib)"
    if [[ -n "$head" ]] && (( head < BUILD_TMPFS_MIN_MIB )); then
        BUILD_TMPFS_WHY="the grant has ${head} MiB of memory headroom, under the ${BUILD_TMPFS_MIN_MIB} MiB a tmpfs build needs"; return 0
    fi
    userdir="$base/nexus-build-$(id -u 2>/dev/null || echo u)"
    if ! mkdir -p "$userdir" 2>/dev/null || ! chmod 700 "$userdir" 2>/dev/null; then
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

# build_tmpfs_cleanup -- remove the directory build_tmpfs_prepare made. Safe to
# call twice and from an EXIT trap.
build_tmpfs_cleanup() {
    if [[ -n "${BUILD_TMPFS_DIR:-}" && "$BUILD_TMPFS_DIR" == */b.* ]]; then
        rm -rf "${BUILD_TMPFS_DIR:?}" 2>/dev/null || true
    fi
    BUILD_TMPFS_DIR=""
    return 0
}
