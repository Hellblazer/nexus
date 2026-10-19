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
unset NX_BUILD_TMPFS NX_JOOQ_CACHE LAB_CPUS BUILD_TMPFS_PROC_CGROUP BUILD_TMPFS_CGROUP_ROOT BUILD_TMPFS_UID
shm="$WORKDIR/shm"; mkdir -p "$shm"
export BUILD_TMPFS_SHM="$shm" BUILD_TMPFS_OS=Linux BUILD_TMPFS_MIN_MIB=1
# Keep the real /proc/self/cgroup out of every case that does not set its own: by
# default a grant whose cgroup has ~100 GiB of room.
okcg="$WORKDIR/okcg"; mkdir -p "$okcg/grant"
echo max > "$okcg/memory.max"; echo 1 > "$okcg/memory.current"
echo 107374182400 > "$okcg/grant/memory.max"; echo 1048576 > "$okcg/grant/memory.current"
printf 'anon 0\nactive_file 0\ninactive_file 0\n' > "$okcg/grant/memory.stat"
export BUILD_TMPFS_PROC_CGROUP='0::/grant'
export BUILD_TMPFS_CGROUP_ROOT="$okcg"
# A jOOQ cache that holds a complete main-* and test-* entry, so the auto decision's
# cache gate passes unless a case breaks it.
jooq="$WORKDIR/jooq"
mk_entry() {  # <root> <main|test> <hash>
    mkdir -p "$1/$2-$3-prod/generated/p"; echo 'class A {}' > "$1/$2-$3-prod/generated/p/A.java"
    echo digest > "$1/$2-$3-prod/complete"
}
mk_entry "$jooq" main aaa; mk_entry "$jooq" test aaa
export NX_JOOQ_CACHE="$jooq"

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
export BUILD_TMPFS_CGROUP_ROOT="$okcg" BUILD_TMPFS_PROC_CGROUP='0::/grant'

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

echo "Test 7 (fail safe): inside a grant an unreadable memory limit is a refusal; an explicit mode warns"
export BUILD_TMPFS_CGROUP_ROOT="$WORKDIR/nocg" BUILD_TMPFS_PROC_CGROUP='0::/'
LAB_CPUS=8 build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"memory limit cannot be read"* ]] && ok "no memory.max on the path, inside a grant: disk ($BUILD_TMPFS_WHY)" || bad "unreadable limit: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
LAB_CPUS=8 NX_BUILD_TMPFS=on build_tmpfs_prepare
[[ -n "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WARN" == *"memory limit cannot be read"* ]] && ok "explicit on inside a grant: tmpfs, with the warning" || bad "explicit on: dir=$BUILD_TMPFS_DIR warn=$BUILD_TMPFS_WARN why=$BUILD_TMPFS_WHY"
build_tmpfs_cleanup
NX_BUILD_TMPFS=on build_tmpfs_prepare
[[ -n "$BUILD_TMPFS_DIR" && -z "$BUILD_TMPFS_WARN" ]] && ok "outside a grant an unreadable limit is not a warning (there is no grant to bound)" || bad "outside a grant: warn=$BUILD_TMPFS_WARN"
build_tmpfs_cleanup
export BUILD_TMPFS_CGROUP_ROOT="$okcg" BUILD_TMPFS_PROC_CGROUP='0::/grant'

echo "Test 8 (fail safe): a BUILD_TMPFS_MIN_MIB that is not a number fails closed, in every mode"
for v in abc 12x -5 1.5; do
    BUILD_TMPFS_MIN_MIB="$v" LAB_CPUS=8 build_tmpfs_prepare
    [[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"not a whole number"* ]] && ok "MIN_MIB='$v': disk" || bad "MIN_MIB='$v' chose '$BUILD_TMPFS_DIR' ($BUILD_TMPFS_WHY)"
done
BUILD_TMPFS_MIN_MIB=abc NX_BUILD_TMPFS=on build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" ]] && ok "explicit on does not go past a bad MIN_MIB" || bad "explicit on chose $BUILD_TMPFS_DIR"

echo "Test 9 (fail safe): the auto decision needs a jOOQ cache that can serve the build"
prep_jooq() { LAB_CPUS=8 build_tmpfs_prepare "$WORKDIR" "$@"; }
prep_jooq
[[ -n "$BUILD_TMPFS_DIR" && -z "$BUILD_TMPFS_WARN" && "$BUILD_TMPFS_JOOQ_ROOT" == "$jooq" ]] && ok "a complete main-* and test-* entry: tmpfs, no warning" || bad "good cache: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
build_tmpfs_cleanup
NX_JOOQ_CACHE="$WORKDIR/jooq-empty" prep_jooq
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"jOOQ cache"* ]] && ok "a missing cache directory: disk ($BUILD_TMPFS_WHY)" || bad "missing cache: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
mkdir -p "$WORKDIR/jooq-empty"
NX_JOOQ_CACHE="$WORKDIR/jooq-empty" prep_jooq
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"no complete main-* and test-* entry"* ]] && ok "an empty cache: disk" || bad "empty cache: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
mk_entry "$WORKDIR/jooq-main-only" main aaa
NX_JOOQ_CACHE="$WORKDIR/jooq-main-only" prep_jooq
[[ -z "$BUILD_TMPFS_DIR" ]] && ok "main-* without test-*: disk" || bad "main-only chose $BUILD_TMPFS_DIR"
mk_entry "$WORKDIR/jooq-torn" main aaa; mk_entry "$WORKDIR/jooq-torn" test aaa; : > "$WORKDIR/jooq-torn/test-aaa-prod/complete"
NX_JOOQ_CACHE="$WORKDIR/jooq-torn" prep_jooq
[[ -z "$BUILD_TMPFS_DIR" ]] && ok "an entry whose complete marker is empty does not count: disk" || bad "torn entry chose $BUILD_TMPFS_DIR"
mk_entry "$WORKDIR/jooq-nojava" main aaa; mk_entry "$WORKDIR/jooq-nojava" test aaa; rm -f "$WORKDIR/jooq-nojava"/test-aaa-prod/generated/p/A.java
NX_JOOQ_CACHE="$WORKDIR/jooq-nojava" prep_jooq
[[ -z "$BUILD_TMPFS_DIR" ]] && ok "an entry with no generated .java does not count: disk" || bad "javaless entry chose $BUILD_TMPFS_DIR"
if [[ "$(id -u)" != 0 ]]; then
    mk_entry "$WORKDIR/jooq-ro" main aaa; mk_entry "$WORKDIR/jooq-ro" test aaa; chmod a-w "$WORKDIR/jooq-ro"
    NX_JOOQ_CACHE="$WORKDIR/jooq-ro" prep_jooq
    [[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"not writable"* ]] && ok "a cache directory we cannot write: disk" || bad "unwritable cache: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
    chmod u+w "$WORKDIR/jooq-ro"
else
    ok "(running as root: the unwritable-cache case does not apply)"
fi
prep_jooq -Dnexus.jooq.cache.dir=off
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"jOOQ cache is off"* ]] && ok "-Dnexus.jooq.cache.dir=off in the Maven arguments: disk" || bad "dir=off arg: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
prep_jooq -Dnexus.jooq.cache.dir=
[[ -z "$BUILD_TMPFS_DIR" ]] && ok "-Dnexus.jooq.cache.dir= (empty, the pom reads it as off): disk" || bad "empty dir arg chose $BUILD_TMPFS_DIR"
prep_jooq -Dnexus.jooq.regenerate=true
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *regenerate* ]] && ok "-Dnexus.jooq.regenerate=true forces the codegen: disk" || bad "regenerate: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
NX_JOOQ_CACHE=off prep_jooq -Dnexus.jooq.cache.dir="$jooq"
[[ -n "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_JOOQ_ROOT" == "$jooq" ]] && ok "the -D wins over NX_JOOQ_CACHE, as in the pom" || bad "-D precedence: dir=$BUILD_TMPFS_DIR root=$BUILD_TMPFS_JOOQ_ROOT"
build_tmpfs_cleanup
# the fallback to the repository's common dir
fb="$WORKDIR/fbrepo"; mkdir -p "$fb"; git -C "$fb" init -q
(unset NX_JOOQ_CACHE; LAB_CPUS=8 build_tmpfs_prepare "$fb"; [[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_JOOQ_ROOT" == "$(cd "$fb" && pwd -P)/.git/nexus-jooq-cache" ]]) && ok "with no NX_JOOQ_CACHE the cache is <common dir>/nexus-jooq-cache (absent here: disk)" || bad "common dir fallback"
(unset NX_JOOQ_CACHE; LAB_CPUS=8 build_tmpfs_prepare "$WORKDIR/not-a-repo"; [[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_JOOQ_ROOT" == off ]]) && ok "not a repository and no NX_JOOQ_CACHE: the cache is off: disk" || bad "no-repo case"
# an explicit mode warns instead
NX_BUILD_TMPFS=on NX_JOOQ_CACHE="$WORKDIR/jooq-empty" build_tmpfs_prepare "$WORKDIR"
[[ -n "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WARN" == *"jOOQ cache"* ]] && ok "explicit on with an empty cache: tmpfs, with the warning" || bad "explicit on: dir=$BUILD_TMPFS_DIR warn=$BUILD_TMPFS_WARN"
build_tmpfs_cleanup
NX_BUILD_TMPFS="$WORKDIR/elsewhere" BUILD_TMPFS_OS=Darwin build_tmpfs_prepare "$WORKDIR" -Dnexus.jooq.cache.dir=off
[[ -n "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WARN" == *"jOOQ cache is off"* ]] && ok "explicit directory with the cache off: tmpfs, with the warning" || bad "explicit dir: dir=$BUILD_TMPFS_DIR warn=$BUILD_TMPFS_WARN"
build_tmpfs_cleanup
printf 'main-aaa-prod\ntest-aaa-prod\n' > "$WORKDIR/want-entries"
mkdir -p "$jooq/main-old.old.9" "$jooq/.staging.x"
[[ "$(build_tmpfs_jooq_entries "$jooq")" == "$(cat "$WORKDIR/want-entries")" ]] && ok "the entry snapshot lists main-*/test-* only (no .old, no .staging)" || bad "entries: $(build_tmpfs_jooq_entries "$jooq")"
rm -rf "$jooq/main-old.old.9" "$jooq/.staging.x"

echo "Test 10 (per-user directory): a symlink, or a directory that is not ours, is refused"
shm2="$WORKDIR/shm2"; mkdir -p "$shm2" "$WORKDIR/target-of-link"
ln -s "$WORKDIR/target-of-link" "$shm2/nexus-build-$(id -u)"
BUILD_TMPFS_SHM="$shm2" LAB_CPUS=8 build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *symlink* ]] && ok "nexus-build-<uid> is a symlink: disk ($BUILD_TMPFS_WHY)" || bad "symlink: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
[[ -z "$(ls -A "$WORKDIR/target-of-link")" ]] && ok "nothing was created through the link" || bad "wrote through the symlink"
# A real directory whose owner is not the uid the name claims (a stand-in for
# another user's directory, which a test cannot create without root).
BUILD_TMPFS_UID=4242 LAB_CPUS=8 build_tmpfs_prepare
[[ -z "$BUILD_TMPFS_DIR" && "$BUILD_TMPFS_WHY" == *"not a directory owned by uid 4242"* ]] && ok "a directory owned by someone else: disk ($BUILD_TMPFS_WHY)" || bad "foreign owner: dir=$BUILD_TMPFS_DIR why=$BUILD_TMPFS_WHY"
rm -rf "$shm/nexus-build-4242"

echo "Test 11 (build_tmpfs_run): runs in its own process group; the exit code and the log come back; stop_child ends the whole group"
rcrun=0; outrun="$(build_tmpfs_run "$WORKDIR/run.log" bash -c 'echo to-stdout; echo to-stderr >&2; exit 7')" || rcrun=$?
[[ $rcrun -eq 7 && "$outrun" == *to-stdout* && "$outrun" == *to-stderr* ]] && ok "rc 7 and both streams come back through the log" || bad "run: rc $rcrun out $outrun"
outrun2="$(build_tmpfs_run "" bash -c 'echo live')"
[[ "$outrun2" == live ]] && ok "without a log the output goes straight through" || bad "no-log run: $outrun2"
kidfile="$WORKDIR/kid"; : > "$kidfile"
set -m; bash -c 'sleep 60 & echo $! > "$1"; wait' _ "$kidfile" </dev/null >/dev/null 2>&1 &
BUILD_TMPFS_CHILD_PID=$!; set +m
for _ in $(seq 1 50); do [[ -s "$kidfile" ]] && break; sleep 0.1; done
kid="$(cat "$kidfile")"
kill -0 "$kid" 2>/dev/null && ok "a grandchild is running in the group" || bad "no grandchild"
[[ "$(ps -o pgid= -p "$BUILD_TMPFS_CHILD_PID" | tr -d ' ')" == "$BUILD_TMPFS_CHILD_PID" ]] && ok "the build leader is its own process group leader" || bad "not a group leader"
build_tmpfs_stop_child
kill -0 "$kid" 2>/dev/null && bad "the grandchild survived stop_child" || ok "stop_child ended the leader and the grandchild"
build_tmpfs_stop_child && ok "a second stop_child is a no-op" || bad "second stop_child failed"

echo "Test 12 (stop_child): a leader that ignores TERM is killed, and stop_child returns within a bound"
set -m; bash -c 'trap "" TERM; while :; do sleep 1; done' </dev/null >/dev/null 2>&1 &
BUILD_TMPFS_CHILD_PID=$!; set +m
stubborn=$BUILD_TMPFS_CHILD_PID
sleep 0.3
t0=$SECONDS
build_tmpfs_stop_child
(( SECONDS - t0 <= 15 )) && ok "stop_child returned within 15 s on a TERM-ignoring leader" || bad "stop_child took $(( SECONDS - t0 )) s"
kill -0 "$stubborn" 2>/dev/null && { bad "the TERM-ignoring leader survived"; kill -KILL "$stubborn" 2>/dev/null; } || ok "the TERM-ignoring leader is gone"

echo
echo "build-tmpfs_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
