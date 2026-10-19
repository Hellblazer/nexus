#!/usr/bin/env bash
# scripts/lib/build-tmpfs_test.sh -- shell tests for build-tmpfs.sh (bead
# nexus-0a1hw). Self-provisioning: a throwaway directory stands in for /dev/shm
# (BUILD_TMPFS_SHM), a fake cgroup tree for /sys/fs/cgroup, and BUILD_TMPFS_OS
# stands in for uname, so the Linux-only decision runs on any host. Never runs
# Maven. Run directly with bash:
#   bash scripts/lib/build-tmpfs_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/build_tmpfs_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
PASS=0; FAIL=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

# shellcheck source=./build-tmpfs.sh disable=SC1091
source "$HERE/build-tmpfs.sh"

# A host environment must not steer the decision under test.
unset NX_BUILD_TMPFS NX_JOOQ_CACHE LAB_CPUS BUILD_TMPFS_PROC_CGROUP BUILD_TMPFS_CGROUP_ROOT
shm="$WORKDIR/shm"; mkdir -p "$shm"
export BUILD_TMPFS_SHM="$shm" BUILD_TMPFS_OS=Linux BUILD_TMPFS_MIN_MIB=1
# Keep the real /proc/self/cgroup out of every case that does not set its own.
export BUILD_TMPFS_PROC_CGROUP='0::/'
export BUILD_TMPFS_CGROUP_ROOT="$WORKDIR/nocg"

userdir="$shm/nexus-build-$(id -u)"

echo "Test 1: auto is off outside a lab-run grant, off on macOS, off with the jOOQ cache off"
build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" ]] && ok "Linux, no LAB_CPUS: build stays on disk" || bad "dir chosen without a grant: $BUILD_TMPFS_DIR"
[[ "$BUILD_TMPFS_WHY" == *LAB_CPUS* ]] && ok "the reason names LAB_CPUS ($BUILD_TMPFS_WHY)" || bad "reason: $BUILD_TMPFS_WHY"
LAB_CPUS=8 BUILD_TMPFS_OS=Darwin build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"not Linux"* ]] && ok "macOS inside a (pretend) grant: disk" || bad "darwin: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
LAB_CPUS=8 NX_JOOQ_CACHE=off build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *NX_JOOQ_CACHE* ]] && ok "NX_JOOQ_CACHE=off: disk (a fresh dir would re-run codegen)" || bad "jooq off: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
[[ ! -e "$userdir" ]] && ok "none of those created anything under the tmpfs root" || bad "tmpfs root touched: $(ls -A "$shm")"

echo "Test 2: Linux inside a grant takes a private per-build directory"
LAB_CPUS=8 build_tmpfs_prepare
d="$BUILD_TMPFS_DIR"
[[ -n "$d" && -d "$d" ]] && ok "directory created ($BUILD_TMPFS_WHY)" || bad "no directory: $BUILD_TMPFS_WHY"
[[ "$d" == "$userdir"/b.$$.* ]] && ok "under <root>/nexus-build-<uid>/b.<pid>.<rand>" || bad "unexpected path $d"
mode="$(stat -c %a "$userdir" 2>/dev/null || stat -f %Lp "$userdir")"
[[ "$mode" == 700 ]] && ok "the per-user directory is 0700" || bad "per-user directory mode $mode"

echo "Test 3: cleanup removes the directory, twice is safe, and it will not delete a stranger"
BUILD_TMPFS_DIR="$d"; build_tmpfs_cleanup
[[ ! -e "$d" ]] && ok "directory removed" || bad "directory survived cleanup"
build_tmpfs_cleanup && ok "a second cleanup is a no-op" || bad "second cleanup failed"
mkdir -p "$WORKDIR/precious"; echo keep > "$WORKDIR/precious/f"
BUILD_TMPFS_DIR="$WORKDIR/precious"; build_tmpfs_cleanup
[[ -f "$WORKDIR/precious/f" ]] && ok "a path that is not a b.* build directory is never deleted" || bad "cleanup deleted a directory it did not make"

echo "Test 4: NX_BUILD_TMPFS=off wins over a grant; on forces outside one; garbage is refused"
LAB_CPUS=8 NX_BUILD_TMPFS=off build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" ]] && ok "off: disk even inside a grant" || bad "off still chose $BUILD_TMPFS_DIR"
NX_BUILD_TMPFS=on build_tmpfs_prepare
[[ -n "$BUILD_TMPFS_DIR" ]] && ok "on: tmpfs outside a grant" || bad "on refused: $BUILD_TMPFS_WHY"
build_tmpfs_cleanup
NX_BUILD_TMPFS=on BUILD_TMPFS_OS=Darwin build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" ]] && ok "on, but macOS has no /dev/shm: disk" || bad "on+darwin chose $BUILD_TMPFS_DIR"
NX_BUILD_TMPFS=bogus build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"not understood"* ]] && ok "an unknown value builds on disk and says so" || bad "bogus: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
NX_BUILD_TMPFS="$WORKDIR/elsewhere" BUILD_TMPFS_OS=Darwin build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" ]] && ok "an explicit directory that does not exist: disk" || bad "missing explicit dir chose $BUILD_TMPFS_DIR"
mkdir -p "$WORKDIR/elsewhere"
NX_BUILD_TMPFS="$WORKDIR/elsewhere" BUILD_TMPFS_OS=Darwin build_tmpfs_prepare
[[ "$BUILD_TMPFS_DIR" == "$WORKDIR/elsewhere/nexus-build-$(id -u)/b.$$."* ]] && ok "an explicit directory is honoured on any OS" || bad "explicit dir: $BUILD_TMPFS_DIR ($BUILD_TMPFS_WHY)"
build_tmpfs_cleanup

echo "Test 5: too little free tmpfs space, or too little grant headroom, builds on disk"
LAB_CPUS=8 BUILD_TMPFS_MIN_MIB=999999999 build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"MiB free"* ]] && ok "free space under the floor: disk ($BUILD_TMPFS_WHY)" || bad "free: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
cg="$WORKDIR/cg"; mkdir -p "$cg/grant"
echo max > "$cg/memory.max"; echo 1 > "$cg/memory.current"
echo 2147483648 > "$cg/grant/memory.max"          # 2048 MiB limit
echo 1932525568 > "$cg/grant/memory.current"      # 205 MiB room
printf 'anon 0\nactive_file 0\ninactive_file 0\n' > "$cg/grant/memory.stat"
export BUILD_TMPFS_CGROUP_ROOT="$cg" BUILD_TMPFS_PROC_CGROUP='0::/grant'
h="$(_build_tmpfs_cgroup_headroom_mib)"
[[ "$h" == 205 ]] && ok "headroom = limit - current ($h MiB)" || bad "headroom $h, expected 205"
LAB_CPUS=8 BUILD_TMPFS_MIN_MIB=300 build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *headroom* ]] && ok "205 MiB of headroom under a 300 MiB floor: disk ($BUILD_TMPFS_WHY)" || bad "headroom: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
printf 'active_file 0\ninactive_file 134217728\n' > "$cg/grant/memory.stat"   # 128 MiB droppable cache
h="$(_build_tmpfs_cgroup_headroom_mib)"
[[ "$h" == 333 ]] && ok "droppable page cache is credited ($h MiB)" || bad "credited headroom $h, expected 333"
LAB_CPUS=8 BUILD_TMPFS_MIN_MIB=300 build_tmpfs_prepare
[[ -n "$BUILD_TMPFS_DIR" ]] && ok "333 MiB credited headroom clears the 300 MiB floor" || bad "refused: $BUILD_TMPFS_WHY"
build_tmpfs_cleanup
echo 9999999999 > "$cg/memory.max"; echo 9999999990 > "$cg/memory.current"   # an ancestor with 9 bytes of room
h="$(_build_tmpfs_cgroup_headroom_mib)"
[[ "$h" == 0 ]] && ok "the tightest limit on the path wins, ancestors included ($h MiB)" || bad "ancestor headroom $h, expected 0"
export BUILD_TMPFS_CGROUP_ROOT="$WORKDIR/nocg" BUILD_TMPFS_PROC_CGROUP='0::/'

echo "Test 6: the sweep removes dead-pid directories older than the guard, and only those"
mkdir -p "$userdir"
dead_pid=999999; while kill -0 "$dead_pid" 2>/dev/null; do dead_pid=$((dead_pid - 1)); done
mkdir -p "$userdir/b.$dead_pid.old" "$userdir/b.$dead_pid.new" "$userdir/b.$$.live" "$userdir/b.notapid.x"
for x in "$userdir/b.$dead_pid.old" "$userdir/b.$$.live" "$userdir/b.notapid.x"; do touch -t 202001010000 "$x"; done
LAB_CPUS=8 BUILD_TMPFS_STALE_MIN=60 build_tmpfs_prepare
[[ ! -e "$userdir/b.$dead_pid.old" ]] && ok "a dead owner's old directory is swept" || bad "stale directory survived"
[[ -d "$userdir/b.$dead_pid.new" ]] && ok "a dead owner's fresh directory is kept (age guard)" || bad "fresh directory was swept"
[[ -d "$userdir/b.$$.live" ]] && ok "a live owner's old directory is kept" || bad "live directory was swept"
[[ -d "$userdir/b.notapid.x" ]] && ok "a name that carries no pid is kept" || bad "unparseable directory was swept"
build_tmpfs_cleanup

echo
echo "build-tmpfs_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
