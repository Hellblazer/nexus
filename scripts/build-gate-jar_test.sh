#!/usr/bin/env bash
# scripts/build-gate-jar_test.sh — script-level test for build-gate-jar.sh
# (bead nexus-g6xpa): a throwaway git repo with a FAKE service/mvnw that
# writes a real (zip) jar, so the miss → build → store → hit path and the
# failure → restore path run end to end without Maven or Docker. Run with:
#   bash scripts/build-gate-jar_test.sh
set -u -o pipefail

# The suite builds its own fake repo and assumes the lease root is that repo's
# own. An inherited NX_BUILD_LEASE_ROOT (CI's lease step exports it, and so does
# the documented hand-run setup on a shared host) would point build-gate-jar.sh's
# lease at the shared root, where a live peer run may hold it: the suite then
# waits on that holder instead of finishing (nexus-mntbl). Scrubbed here, like the
# suites that already do, and pinned by test_shell_suite_wiring.py's
# TestSuitesIgnoreAnInheritedLeaseRoot.
unset NX_BUILD_LEASE_ROOT NX_SUITE_LEASE_WAIT NX_SUITE_LEASE_HELD_BY LAB_CPUS GITHUB_ACTIONS

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/build_gate_jar_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
PASS=0; FAIL=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

repo="$WORKDIR/repo"
props_rel="service/src/main/resources/META-INF/nexus/release.properties"
mkdir -p "$repo/scripts/lib" "$repo/service/src/main/resources/META-INF/nexus" "$repo/src/nexus"
cp "$HERE/build-gate-jar.sh" "$repo/scripts/"
cp "$HERE/lib/build-lease.sh" "$HERE/lib/release-props-lease.sh" "$HERE/lib/gate-jar-cache.sh" "$HERE/lib/build-tmpfs.sh" "$repo/scripts/lib/"
chmod +x "$repo/scripts/build-gate-jar.sh"
printf 'release_version=\nbuild_ref=\nname=nexus\n' > "$repo/$props_rel"
echo 'REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 107)' > "$repo/src/nexus/engine_version.py"
echo 'class A {}' > "$repo/service/src/main/A.java"
printf 'service/target/\nservice/.build-lease/\n' > "$repo/.gitignore"
# Fake mvnw: MVNW_FAIL=1 fails; otherwise writes a real jar whose payload
# is the stamped release.properties it saw, and counts its invocations. It
# writes where -Dnexus.build.dir says (default ./target, as the real pom does),
# next to a classes/ file standing in for the rest of a build directory, and
# records its arguments and build directory in $MVNW_ARGS / $MVNW_BUILDDIR.
cat > "$repo/service/mvnw" <<'STUB'
#!/usr/bin/env bash
echo run >> "$MVNW_COUNT"
echo "$*" >> "$MVNW_ARGS"
bd=target
for a in "$@"; do case "$a" in -Dnexus.build.dir=*) bd="${a#-Dnexus.build.dir=}" ;; esac; done
case "$bd" in /*) ;; *) bd="$PWD/$bd" ;; esac
echo "$bd" >> "$MVNW_BUILDDIR"
# A tmpfs build (one given -Dnexus.build.dir) that the grant's memory killed, or that ran out of space.
case " $* " in *" -Dnexus.build.dir="*)
    [[ -n "${MVNW_TMPFS_RC:-}" ]] && exit "$MVNW_TMPFS_RC"
    [[ "${MVNW_TMPFS_ENOSPC:-0}" == 1 ]] && { echo "java.io.IOException: No space left on device" >&2; exit 1; }
esac
[[ "${MVNW_FAIL:-0}" == 1 ]] && exit 1
# A build that never ends and keeps re-creating its build directory, with a child, as a real Maven does.
if [[ "${MVNW_HANG:-0}" == 1 ]]; then
    echo $$ > "$MVNW_HANG_PID"
    sleep 300 &
    echo $! > "$MVNW_HANG_KID"
    while :; do mkdir -p "$bd/classes"; sleep 0.2; done
fi
# The jOOQ codegen publishing a new entry into the shared cache.
if [[ -n "${MVNW_CODEGEN_ROOT:-}" ]]; then
    mkdir -p "$MVNW_CODEGEN_ROOT/main-newhash-prod/generated" "$MVNW_CODEGEN_ROOT/test-newhash-prod/generated"
    echo 'class G {}' > "$MVNW_CODEGEN_ROOT/main-newhash-prod/generated/G.java"
    echo digest > "$MVNW_CODEGEN_ROOT/main-newhash-prod/complete"
fi
# A peer that stores a complete entry for this key, carrying ANOTHER build_ref, before this run's store.
if [[ -n "${MVNW_PEER_ENTRY:-}" ]]; then
    mkdir -p "$MVNW_PEER_ENTRY"
    rm -f "$MVNW_PEER_ENTRY/jar"
    python3 - "$MVNW_PEER_ENTRY/jar" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as zf:
    zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    zf.writestr("META-INF/nexus/release.properties", "release_version=0.1.107\nbuild_ref=peer+9-9\n")
PY
    echo "peer+9-9" > "$MVNW_PEER_ENTRY/build_ref"
    chmod a-w "$MVNW_PEER_ENTRY/jar"
fi
mkdir -p "$bd/classes"
echo junk > "$bd/classes/Junk.class"
# Worst case for a hard-linked jar: a writer that rewrites the existing inode in
# place and does not respect its read-only mode (root, or a plugin that chmods).
chmod u+w "$bd/nexus-service-1.0-SNAPSHOT.jar" 2>/dev/null || true
python3 - "$bd/nexus-service-1.0-SNAPSHOT.jar" "$PWD/src/main/resources/META-INF/nexus/release.properties" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as zf:
    zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    zf.write(sys.argv[2], "META-INF/nexus/release.properties")
PY
STUB
chmod +x "$repo/service/mvnw"
git -C "$repo" init -q && git -C "$repo" config user.email t@t && git -C "$repo" config user.name t
git -C "$repo" add -A && git -C "$repo" commit -qm base
export MVNW_COUNT="$WORKDIR/count"; : > "$MVNW_COUNT"
export MVNW_ARGS="$WORKDIR/mvnw-args"; : > "$MVNW_ARGS"
export MVNW_BUILDDIR="$WORKDIR/mvnw-builddir"; : > "$MVNW_BUILDDIR"
# A host's own settings must not steer the build-directory decision under test.
unset NX_BUILD_TMPFS BUILD_TMPFS_SHM BUILD_TMPFS_OS BUILD_TMPFS_MIN_MIB BUILD_TMPFS_PROC_CGROUP BUILD_TMPFS_CGROUP_ROOT BUILD_TMPFS_UID NX_JOOQ_CACHE MVNW_TMPFS_RC MVNW_TMPFS_ENOSPC MVNW_HANG MVNW_CODEGEN_ROOT MVNW_PEER_ENTRY
export NX_GATE_JAR_CACHE="$WORKDIR/cache"

echo "Test 1: a failing build restores release.properties and stores nothing"
out1="$(cd "$repo" && MVNW_FAIL=1 scripts/build-gate-jar.sh 2>&1)"; rc1=$?
[[ $rc1 -ne 0 ]] && ok "failed build exits non-zero (rc $rc1)" || bad "failed build exited 0"
[[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "release.properties restored byte-for-byte" || bad "release.properties not restored: $(cat "$repo/$props_rel")"
[[ -z "$(ls -A "$WORKDIR/cache" 2>/dev/null | grep -v '^\.')" ]] && ok "nothing cached after a failed build" || bad "cache has entries after a failed build"
[[ "$out1" == *"MISS"* ]] && ok "failed run reported a cache miss" || bad "no MISS line: $out1"

echo "Test 2: miss builds and stores; the jar carries the stamp"
out2="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc2=$?
[[ $rc2 -eq 0 ]] && ok "build succeeded" || bad "build rc $rc2: $out2"
[[ "$out2" == *"MISS"* && "$out2" == *"built "* ]] && ok "miss then built" || bad "unexpected output: $out2"
ref2="$(printf '%s\n' "$out2" | sed -n 's/^stamped build_ref=//p')"
[[ -n "$ref2" ]] && ok "build_ref printed ($ref2)" || bad "no build_ref line"
python3 - "$repo/service/target/nexus-service-1.0-SNAPSHOT.jar" "$ref2" <<'PY' && ok "jar payload carries release_version=0.1.107 and the printed build_ref" || bad "jar payload lacks the stamp"
import sys, zipfile
p = zipfile.ZipFile(sys.argv[1]).read("META-INF/nexus/release.properties").decode()
assert "release_version=0.1.107\n" in p and f"build_ref={sys.argv[2]}\n" in p, p
PY
[[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "release.properties restored after a successful build" || bad "props dirty after success"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == 2 ]] && ok "mvnw invoked twice so far (fail + build)" || bad "mvnw count $(cat "$MVNW_COUNT" | wc -l)"

echo "Test 3: identical service/ content hits; mvnw is not invoked; same build_ref; fresh mtime"
rm -f "$repo/service/target/nexus-service-1.0-SNAPSHOT.jar"
sleep 1
out3="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc3=$?
[[ $rc3 -eq 0 && "$out3" == *"HIT"* ]] && ok "cache HIT" || bad "no HIT (rc $rc3): $out3"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == 2 ]] && ok "mvnw NOT invoked on a hit" || bad "mvnw ran on a hit"
[[ "$out3" == *"stamped build_ref=$ref2"* ]] && ok "hit reprints the cached build_ref" || bad "hit printed a different build_ref: $out3"
[[ -f "$repo/service/target/nexus-service-1.0-SNAPSHOT.jar" ]] && ok "jar copied into service/target" || bad "no jar after hit"
if [[ "$repo/service/target/.gate-jar-key" -nt "$repo/service/src/main/A.java" ]]; then ok "the key marker is newer than the sources (the per-worktree freshness signal)"; else bad "key marker older than sources"; fi
[[ "$out3" == *"built "* ]] && ok "hit prints the built line callers grep for" || bad "no built line on hit"

echo "Test 4: a leftover stamp in release.properties REFUSES (nexus-iexvl guard) instead of silently serving a cache hit"
# Before nexus-iexvl this scenario ("does not defeat the key") was treated
# as tolerable: a dirty tree still served a HIT. That is exactly the
# incident this bead fixes -- a dirty tree with the lease free means a
# PRIOR process left it stamped without holding this same lease for its
# whole stamp lifetime, and silently building on top of it is how a
# concurrent gate's leftover stamp went unnoticed. The correct behavior
# now is a loud refusal, not a quiet HIT.
printf 'release_version=9.9.9\nbuild_ref=stale+1-2\nname=nexus\n' > "$repo/$props_rel"
out4="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc4=$?
[[ $rc4 -eq 75 ]] && ok "leftover stamp refuses (rc 75)" || bad "leftover stamp returned rc $rc4 (expected 75): $out4"
[[ "$out4" == *"REFUSED"* ]] && ok "refusal is disclosed (REFUSED)" || bad "no REFUSED text: $out4"
[[ "$out4" == *"release_version=9.9.9"* ]] && ok "refusal shows the offending stamped value" || bad "refusal does not show the stamped value: $out4"
[[ ! -d "$repo/.git/nexus-build-lease/service" ]] && ok "lease not left held after the refusal" || bad "lease still held after the refusal"
git -C "$repo" checkout -q -- "$props_rel"
out4b="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc4b=$?
[[ $rc4b -eq 0 && "$out4b" == *"HIT"* ]] && ok "a clean tree after git checkout serves a HIT again" || bad "clean tree after checkout did not HIT (rc $rc4b): $out4b"

echo "Test 4c (nexus-fkhd3): a service/src/test-only change still HITS; a pom change MISSES"
before4c="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
mkdir -p "$repo/service/src/test/java" "$repo/service/docs"
echo 'class T {}' > "$repo/service/src/test/java/T.java"
echo 'notes' > "$repo/service/README.md"
out4c="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc4c=$?
[[ $rc4c -eq 0 && "$out4c" == *"HIT"* ]] && ok "a src/test + README change HITS" || bad "test-only change did not HIT (rc $rc4c): $out4c"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$before4c" ]] && ok "mvnw NOT invoked for a test-only change" || bad "mvnw ran for a test-only change"
rm -rf "$repo/service/src/test" "$repo/service/README.md" "$repo/service/docs"
echo '<project/>' > "$repo/service/pom.xml"
out4d="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc4d=$?
[[ $rc4d -eq 0 && "$out4d" == *"MISS"* ]] && ok "a new pom.xml MISSES and rebuilds" || bad "pom change did not miss (rc $rc4d): $out4d"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((before4c + 1))" ]] && ok "mvnw ran once for the pom change" || bad "mvnw count after pom change: $(wc -l < "$MVNW_COUNT")"
rm -f "$repo/service/pom.xml"

echo "Test 5: a service/ change misses and rebuilds; NX_GATE_JAR_CACHE=off always builds"
echo 'class B {}' > "$repo/service/src/main/B.java"
out5="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc5=$?
[[ $rc5 -eq 0 && "$out5" == *"MISS"* ]] && ok "changed service/ misses" || bad "expected MISS: $out5"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == 4 ]] && ok "mvnw rebuilt" || bad "mvnw count $(wc -l < "$MVNW_COUNT")"
out6="$(cd "$repo" && NX_GATE_JAR_CACHE=off scripts/build-gate-jar.sh 2>&1)"; rc6=$?
[[ $rc6 -eq 0 && "$out6" == *"MISS"* && "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == 5 ]] && ok "cache off: builds every time" || bad "cache off misbehaved: $out6"

echo "Test 5b (nexus-fkhd3 review): a cache that cannot be written does not fail the build"
echo 'class C {}' > "$repo/service/src/main/C.java"
echo "not a directory" > "$WORKDIR/cache-is-a-file"
before5b="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out5b="$(cd "$repo" && NX_GATE_JAR_CACHE="$WORKDIR/cache-is-a-file/cache" scripts/build-gate-jar.sh 2>&1)"; rc5b=$?
[[ $rc5b -eq 0 ]] && ok "the build exits 0 although the cache cannot be created" || bad "unwritable cache failed the build (rc $rc5b): $out5b"
[[ "$out5b" == *"MISS"* && "$out5b" == *"built "* ]] && ok "it still builds and prints the built line" || bad "output: $out5b"
[[ "$out5b" == *"gate jar cache"*"not"* ]] && ok "and says the cache was not written" || bad "no warning about the cache: $out5b"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((before5b + 1))" ]] && ok "mvnw ran once" || bad "mvnw count after the unwritable-cache build: $(wc -l < "$MVNW_COUNT")"
[[ -f "$repo/service/target/nexus-service-1.0-SNAPSHOT.jar" ]] && ok "the jar is in service/target" || bad "no jar after the unwritable-cache build"
[[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "release.properties restored" || bad "props dirty after the unwritable-cache build"
rm -f "$repo/service/src/main/C.java"

# shellcheck source=./lib/gate-jar-cache.sh disable=SC1091
source "$HERE/lib/gate-jar-cache.sh"
tjar="$repo/service/target/nexus-service-1.0-SNAPSHOT.jar"
sha_of() { shasum -a 256 "$1" | cut -c1-64; }
ino_of() { ls -i "$1" | awk '{print $1}'; }
mtime_of() { python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns)' "$1"; }

echo "Test 7 (nexus-k6zxp): a HIT links the cached jar (no copy); a repeat HIT touches nothing; a later MISS leaves the entry's bytes alone"
key7="$(gate_jar_cache_key "$repo" 0.1.107 "")"; entry7="$WORKDIR/cache/$key7"
[[ -d "$entry7" ]] && ok "the entry for the current inputs exists" || bad "no entry at $entry7"
cnt7="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out7a="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc7a=$?
[[ $rc7a -eq 0 && "$out7a" == *"HIT"* && "$out7a" == *"jar link"* ]] && ok "HIT installs by link" || bad "no linked HIT (rc $rc7a): $out7a"
[[ "$tjar" -ef "$entry7/jar" ]] && ok "service/target's jar IS the entry's inode: a HIT wrote no jar bytes" || bad "target jar is a copy"
[[ "$(cat "$repo/service/target/.gate-jar-key")" == "$key7" ]] && ok "the key is recorded beside the jar" || bad "no key marker"
[[ -z "$(ls -A "$repo/service/target" | grep -F .gate-jar.tmp)" ]] && ok "no temp jar left in service/target" || bad "temp jar left behind"
sum7="$(sha_of "$entry7/jar")"; ref7="$(cat "$entry7/build_ref")"; ino7="$(ino_of "$tjar")"
touch -t 202101010000 "$tjar"; stale7="$WORKDIR/stale7"; touch -t 202101010000 "$stale7"
touch -t 202001010000 "$repo/service/target/.gate-jar-key"; jarm7="$(mtime_of "$tjar")"
out7b="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc7b=$?
[[ $rc7b -eq 0 && "$out7b" == *"HIT"* && "$out7b" == *"jar kept"* ]] && ok "a repeat HIT keeps the jar already in service/target" || bad "repeat HIT did not keep it (rc $rc7b): $out7b"
[[ "$(ino_of "$tjar")" == "$ino7" && "$(sha_of "$entry7/jar")" == "$sum7" ]] && ok "same inode, same bytes after the repeat HIT" || bad "repeat HIT replaced or altered the jar"
[[ "$repo/service/target/.gate-jar-key" -nt "$stale7" ]] && ok "the key marker's mtime is refreshed (per-worktree freshness signal)" || bad "marker is stale after a repeat HIT"
[[ "$(mtime_of "$tjar")" == "$jarm7" ]] && ok "the shared jar's own mtime is NOT touched (another worktree's freshness must not move)" || bad "a HIT touched the shared inode's mtime"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$cnt7" ]] && ok "mvnw not invoked by either HIT" || bad "mvnw ran on a HIT"
echo 'class D {}' > "$repo/service/src/main/D.java"
out7c="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc7c=$?
[[ $rc7c -eq 0 && "$out7c" == *"MISS"* && "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((cnt7 + 1))" ]] && ok "a changed input MISSES and builds while service/target holds a linked jar" || bad "MISS after a linked HIT failed (rc $rc7c): $out7c"
[[ "$(sha_of "$entry7/jar")" == "$sum7" && "$(cat "$entry7/build_ref")" == "$ref7" ]] && ok "the MISS build did not alter the old cache entry's bytes" || bad "the old entry was rewritten through the shared inode"
_gate_jar_cache_entry_complete "$entry7" && ok "the old entry still verifies" || bad "old entry no longer verifies"
[[ ! "$tjar" -ef "$entry7/jar" ]] && ok "the rebuilt jar is a new inode" || bad "rebuilt jar still shares the old entry's inode"
rm -f "$repo/service/src/main/D.java"
out7d="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc7d=$?
[[ $rc7d -eq 0 && "$out7d" == *"HIT"* && "$out7d" == *"stamped build_ref=$ref7"* && "$tjar" -ef "$entry7/jar" ]] && ok "reverting the input HITs the old entry again, byte-for-byte" || bad "old entry not served after the revert (rc $rc7d): $out7d"

echo "Test 8 (nexus-k6zxp): a HIT whose entry has an EMPTY build_ref is a MISS (mvnw runs) and the entry is repaired"
echo 'class E {}' > "$repo/service/src/main/E.java"
(cd "$repo" && scripts/build-gate-jar.sh >/dev/null 2>&1)
key8="$(gate_jar_cache_key "$repo" 0.1.107 "")"; entry8="$WORKDIR/cache/$key8"
[[ -f "$entry8/jar" && -s "$entry8/build_ref" ]] && ok "seeded: a complete entry for this key" || bad "no seeded entry"
printf '' > "$entry8/build_ref"
rm -f "$repo/service/target/.gate-jar-key"
cnt8="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out8="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc8=$?
[[ $rc8 -eq 0 && "$out8" == *"MISS"* && "$out8" != *"HIT key"* ]] && ok "an entry with no build_ref is not served" || bad "served an entry with an empty build_ref (rc $rc8): $out8"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((cnt8 + 1))" ]] && ok "mvnw was invoked" || bad "mvnw not invoked for the empty-build_ref entry"
[[ "$out8" == *"could not be used whole"* ]] && ok "the refusal is disclosed" || bad "no 'could not be used whole': $out8"
[[ -s "$entry8/build_ref" ]] && ok "the entry was repaired with a build_ref" || bad "entry still has an empty build_ref"
out8b="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"
[[ "$out8b" == *"HIT"* ]] && ok "the repaired entry HITS" || bad "repaired entry does not hit: $out8b"
rm -f "$repo/service/src/main/E.java"

echo "Test 9 (nexus-k6zxp): the gate's bare rebuild works after a build-gate-jar.sh hit and after a miss+store"
# tests/e2e/local-service-gate.sh rebuilds with a bare `./mvnw -q package` while holding the lease. The real jar
# plugin fails on a read-only jar ("...jar is read-only"); this builder does the same, so the sequence the gate
# runs (gate_jar_cache_unshare, then the build) is exercised against the state build-gate-jar.sh leaves behind.
strict9="$WORKDIR/mvnw-strict"
cat > "$strict9" <<'STUB'
#!/usr/bin/env bash
j=target/nexus-service-1.0-SNAPSHOT.jar
if [[ -e "$j" && ! -w "$j" ]]; then echo "ERROR: jar is read-only: $j" >&2; exit 1; fi
mkdir -p target
python3 - "$j" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as zf:
    zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
PY
STUB
chmod +x "$strict9"
gate_rebuild() { (cd "$repo/service" && "$strict9" >/dev/null 2>&1); }
for how9 in hit store; do
    if [[ "$how9" == store ]]; then echo 'class G {}' > "$repo/service/src/main/G.java"; fi
    out9="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"
    key9="$(gate_jar_cache_key "$repo" 0.1.107 "")"; entry9="$WORKDIR/cache/$key9"
    [[ ( "$how9" == hit && "$out9" == *"HIT"* ) || ( "$how9" == store && "$out9" == *"MISS"* ) ]] && ok "[$how9] build-gate-jar.sh ran as a $how9" || bad "[$how9] unexpected run: $out9"
    [[ "$tjar" -ef "$entry9/jar" ]] && ok "[$how9] service/target's jar is shared with the entry" || bad "[$how9] jar not shared"
    sum9="$(sha_of "$entry9/jar")"
    if [[ "$(id -u)" != 0 ]]; then
        gate_rebuild && bad "[$how9] control: a bare rebuild WITHOUT unshare succeeded on a read-only linked jar" || ok "[$how9] control: a bare rebuild without unshare fails, as the real jar plugin does"
    fi
    gate_jar_cache_unshare "$tjar"
    gate_rebuild && ok "[$how9] the gate's sequence (unshare, then rebuild) succeeds" || bad "[$how9] rebuild failed after unshare"
    [[ "$(sha_of "$entry9/jar")" == "$sum9" ]] && ok "[$how9] the cache entry's bytes are untouched" || bad "[$how9] the entry changed"
done
rm -f "$repo/service/src/main/G.java"

echo "Test 10 (nexus-k6zxp): an entry whose jar is valid but carries ANOTHER build_ref is a MISS that discards it"
echo 'class H {}' > "$repo/service/src/main/H.java"
(cd "$repo" && scripts/build-gate-jar.sh >/dev/null 2>&1)
key10="$(gate_jar_cache_key "$repo" 0.1.107 "")"; entry10="$WORKDIR/cache/$key10"
ref10="$(cat "$entry10/build_ref")"
[[ -s "$entry10/build_ref" ]] && ok "seeded: a complete entry" || bad "no seeded entry"
# what root in a container would leave: the entry's inode rewritten into a different, still-valid jar
rm -f "$tjar" "$repo/service/target/.gate-jar-key" "$entry10/jar"
python3 - "$entry10/jar" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as zf:
    zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    zf.writestr("META-INF/nexus/release.properties", "release_version=0.1.107\nbuild_ref=someone-elses+1-2\n")
PY
cnt10="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out10="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc10=$?
[[ $rc10 -eq 0 && "$out10" == *"MISS"* && "$out10" != *"HIT key"* ]] && ok "the mismatching entry is not served" || bad "served a jar stamped with another build_ref (rc $rc10): $out10"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((cnt10 + 1))" ]] && ok "mvnw rebuilt" || bad "mvnw not invoked"
[[ "$out10" == *"discarding unusable cache entry"* ]] && ok "the discard is disclosed" || bad "no discard message: $out10"
new10="$(cat "$entry10/build_ref")"
[[ "$new10" != "$ref10" && -s "$entry10/jar" ]] && python3 - "$entry10/jar" "$new10" <<'PY' && ok "the entry now holds the rebuilt jar and its own build_ref" || bad "entry not repaired"
import sys, zipfile
p = zipfile.ZipFile(sys.argv[1]).read("META-INF/nexus/release.properties").decode()
assert f"build_ref={sys.argv[2]}\n" in p, p
PY
rm -f "$repo/service/src/main/H.java"

echo "Test 11 (nexus-0a1hw): on a lab host the build directory is tmpfs and only the stamped jar reaches service/target"
shm="$WORKDIR/shm"; mkdir -p "$shm"
export BUILD_TMPFS_MIN_MIB=1
last_builddir() { tail -n 1 "$MVNW_BUILDDIR"; }
last_args() { tail -n 1 "$MVNW_ARGS"; }
echo 'class J {}' > "$repo/service/src/main/J.java"
rm -rf "$repo/service/target"
cnt11="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out11="$(cd "$repo" && NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc11=$?
[[ $rc11 -eq 0 && "$out11" == *"MISS"* && "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((cnt11 + 1))" ]] && ok "a miss builds once" || bad "tmpfs miss (rc $rc11): $out11"
[[ "$(last_builddir)" == "$shm/nexus-build-$(id -u)/b."* ]] && ok "Maven was told to build under the tmpfs root ($(last_builddir))" || bad "build dir was $(last_builddir)"
[[ "$(last_args)" == *"-Dnexus.build.dir=$shm/"* && "$(last_args)" == *"-Dnexus.jlink.skip=true"* && "$(last_args)" == *"-DskipTests"* ]] && ok "the build got -Dnexus.build.dir, -Dnexus.jlink.skip=true and -DskipTests" || bad "build args: $(last_args)"
[[ "$out11" == *"gate jar build directory: $shm/"*"(tmpfs;"* ]] && ok "the output says where it built" || bad "no build-directory line: $out11"
key11="$(gate_jar_cache_key "$repo" 0.1.107 "")"; entry11="$WORKDIR/cache/$key11"
ref11="$(printf '%s\n' "$out11" | sed -n 's/^stamped build_ref=//p')"
[[ -f "$tjar" && "$tjar" -ef "$entry11/jar" ]] && ok "service/target holds the cache entry's jar (a link, not a second copy)" || bad "target jar is not the entry's"
[[ "$(cat "$repo/service/target/.gate-jar-key")" == "$key11" ]] && ok "the key marker is in service/target" || bad "no key marker in service/target"
[[ ! -e "$repo/service/target/classes" && "$(find "$repo/service/target" -mindepth 1 -maxdepth 1 ! -name nexus-service-1.0-SNAPSHOT.jar ! -name .gate-jar-key | wc -l | tr -d ' ')" == 0 ]] && ok "service/target holds only the jar and its marker: no classes, no build output" || bad "build output reached service/target: $(ls -A "$repo/service/target")"
[[ -z "$(ls -A "$shm/nexus-build-$(id -u)" 2>/dev/null)" ]] && ok "the tmpfs build directory is gone after the run" || bad "tmpfs directory left behind: $(ls -A "$shm/nexus-build-$(id -u)")"
python3 - "$tjar" "$ref11" <<'PY' && ok "the jar carries the build_ref this run printed" || bad "jar build_ref differs from the printed one"
import sys, zipfile
p = zipfile.ZipFile(sys.argv[1]).read("META-INF/nexus/release.properties").decode()
assert f"build_ref={sys.argv[2]}\n" in p, p
PY
[[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "release.properties restored" || bad "props dirty"
cnt11b="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
rm -f "$tjar" "$repo/service/target/.gate-jar-key"
out11b="$(cd "$repo" && NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"
[[ "$out11b" == *"HIT"* && "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$cnt11b" && "$out11b" != *"build directory"* ]] && ok "the next run HITS the stored entry and makes no build directory" || bad "hit after a tmpfs build: $out11b"

echo "Test 11b (nexus-0a1hw): with no cache the jar still lands in service/target, as a private copy"
echo 'class K {}' > "$repo/service/src/main/K.java"
rm -rf "$repo/service/target"
out11c="$(cd "$repo" && NX_GATE_JAR_CACHE=off NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc11c=$?
ref11c="$(printf '%s\n' "$out11c" | sed -n 's/^stamped build_ref=//p')"
[[ $rc11c -eq 0 && -f "$tjar" && "$(last_builddir)" == "$shm/"* ]] && ok "cache off: built in tmpfs, jar placed in service/target" || bad "cache-off tmpfs build (rc $rc11c): $out11c"
python3 - "$tjar" "$ref11c" <<'PY' && ok "the placed jar carries this run's build_ref" || bad "placed jar has another build_ref"
import sys, zipfile
p = zipfile.ZipFile(sys.argv[1]).read("META-INF/nexus/release.properties").decode()
assert f"build_ref={sys.argv[2]}\n" in p, p
PY
[[ ! -e "$repo/service/target/.gate-jar-key" && ! -e "$repo/service/target/classes" ]] && ok "no key marker (nothing cached) and no build output in service/target" || bad "unexpected files: $(ls -A "$repo/service/target")"
[[ -z "$(ls -A "$shm/nexus-build-$(id -u)" 2>/dev/null)" ]] && ok "tmpfs directory removed" || bad "tmpfs directory left behind"
echo 'class L {}' > "$repo/service/src/main/L.java"
rm -rf "$repo/service/target"
out11d="$(cd "$repo" && NX_GATE_JAR_CACHE="$WORKDIR/cache-is-a-file/cache" NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc11d=$?
[[ $rc11d -eq 0 && -f "$tjar" && "$out11d" == *"not written"* ]] && ok "an unwritable cache: the build exits 0, warns, and the jar is placed" || bad "unwritable cache with tmpfs (rc $rc11d): $out11d"

echo "Test 11c (nexus-0a1hw): a failed tmpfs build cleans up, restores the stamp and says how to build on disk"
echo 'class M {}' > "$repo/service/src/main/M.java"
out11e="$(cd "$repo" && MVNW_FAIL=1 NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc11e=$?
[[ $rc11e -ne 0 && "$out11e" == *"NX_BUILD_TMPFS=off"* ]] && ok "failure exits non-zero and names NX_BUILD_TMPFS=off" || bad "failed tmpfs build (rc $rc11e): $out11e"
[[ -z "$(ls -A "$shm/nexus-build-$(id -u)" 2>/dev/null)" ]] && ok "the tmpfs directory is removed on failure" || bad "tmpfs directory left after a failed build"
[[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "release.properties restored after the failure" || bad "props dirty after a failed tmpfs build"

echo "Test 11d (nexus-0a1hw): the default build directory is unchanged outside a lab host; a caller cannot set its own"
svc_target="$(cd "$repo/service" && pwd)/target"   # the stub reports the shell's normalized $PWD
out11f="$(cd "$repo" && BUILD_TMPFS_OS=Linux scripts/build-gate-jar.sh 2>&1)"; rc11f=$?
[[ $rc11f -eq 0 && "$(last_builddir)" == "$svc_target" && "$(last_args)" != *"-Dnexus.build.dir"* ]] && ok "no NX_BUILD_TMPFS, no grant: Maven builds in service/target with no -Dnexus.build.dir" || bad "default build dir $(last_builddir); args $(last_args)"
[[ "$out11f" == *"gate jar build directory: service/target (not inside a lab-run grant"* ]] && ok "and says why" || bad "default output: $out11f"
[[ "$(last_args)" == *"-Dnexus.jlink.skip=true"* ]] && ok "the unused jlink image is skipped on every host" || bad "jlink not skipped: $(last_args)"
echo 'class N {}' > "$repo/service/src/main/N.java"
(cd "$repo" && NX_BUILD_TMPFS=off LAB_CPUS=8 NX_BUILD_LEASE_WAIT=0 scripts/build-gate-jar.sh >/dev/null 2>&1); rc11g=$?
[[ $rc11g -eq 0 && "$(last_builddir)" == "$svc_target" ]] && ok "NX_BUILD_TMPFS=off keeps service/target inside a grant" || bad "off inside a grant (rc $rc11g): dir $(last_builddir)"
cnt11h="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out11h="$(cd "$repo" && scripts/build-gate-jar.sh -Dnexus.build.dir=/elsewhere 2>&1)"; rc11h=$?
[[ $rc11h -eq 64 && "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$cnt11h" ]] && ok "a caller's own -Dnexus.build.dir is refused (64) before anything runs" || bad "caller build dir (rc $rc11h): $out11h"
rm -f "$repo/service/src/main/J.java" "$repo/service/src/main/K.java" "$repo/service/src/main/L.java" "$repo/service/src/main/M.java" "$repo/service/src/main/N.java"

echo "Test 12 (nexus-0a1hw review): a peer stores the same key with ANOTHER build_ref before this run's store; the placed jar must be this run's"
echo 'class P {}' > "$repo/service/src/main/P.java"
rm -rf "$repo/service/target"
key12="$(gate_jar_cache_key "$repo" 0.1.107 "")"; entry12="$WORKDIR/cache/$key12"
rm -rf "$entry12"
out12="$(cd "$repo" && MVNW_PEER_ENTRY="$entry12" NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc12=$?
ref12="$(printf '%s\n' "$out12" | sed -n 's/^stamped build_ref=//p')"
[[ $rc12 -eq 0 && "$out12" == *MISS* ]] && ok "the run succeeds" || bad "peer-race run (rc $rc12): $out12"
[[ "$out12" == *"(private)"* ]] && ok "the output says the jar was placed as a private copy" || bad "no (private): $out12"
python3 - "$tjar" "$ref12" <<'PY' && ok "service/target's jar carries THIS run's build_ref ($ref12)" || bad "service/target's jar has another build_ref"
import sys, zipfile
p = zipfile.ZipFile(sys.argv[1]).read("META-INF/nexus/release.properties").decode()
assert f"build_ref={sys.argv[2]}\n" in p and "peer+9-9" not in p, p
PY
python3 - "$entry12/jar" <<'PY' && [[ "$(cat "$entry12/build_ref")" == "peer+9-9" && ! "$tjar" -ef "$entry12/jar" ]] && ok "the peer's entry is untouched: its own jar and build_ref, not linked to service/target" || bad "the peer's entry was altered or linked"
import sys, zipfile
p = zipfile.ZipFile(sys.argv[1]).read("META-INF/nexus/release.properties").decode()
assert "build_ref=peer+9-9\n" in p, p
PY
[[ ! -e "$repo/service/target/.gate-jar-key" ]] && ok "no key marker claims the peer's entry for this jar" || bad "a key marker was left beside a private jar"
rm -f "$repo/service/src/main/P.java"

echo "Test 13 (nexus-0a1hw review): a tmpfs build killed for memory (137) or out of space is retried once on disk; any other failure is not"
for how13 in rc137 enospc; do
    echo "class Q_$how13 {}" > "$repo/service/src/main/Q_$how13.java"   # a new key each time: a hit would run no build
    rm -rf "$repo/service/target"
    cnt13="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
    if [[ "$how13" == rc137 ]]; then
        out13="$(cd "$repo" && MVNW_TMPFS_RC=137 NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc13=$?
    else
        out13="$(cd "$repo" && MVNW_TMPFS_ENOSPC=1 NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc13=$?
    fi
    [[ $rc13 -eq 0 ]] && ok "[$how13] the run succeeds after the retry" || bad "[$how13] rc $rc13: $out13"
    [[ "$out13" == *"retrying once on disk"* ]] && ok "[$how13] the retry is printed" || bad "[$how13] no retry line: $out13"
    [[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((cnt13 + 2))" && "$(last_builddir)" == "$svc_target" && "$(last_args)" != *"-Dnexus.build.dir"* ]] && ok "[$how13] Maven ran twice, the second time in service/target" || bad "[$how13] count/dir: $(last_builddir) / $(last_args)"
    [[ -f "$tjar" && -z "$(ls -A "$shm/nexus-build-$(id -u)" 2>/dev/null)" ]] && ok "[$how13] the jar is in service/target and the tmpfs directory is gone" || bad "[$how13] jar or tmpfs leftovers"
    [[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "[$how13] release.properties restored" || bad "[$how13] props dirty"
done
echo 'class Q_both {}' > "$repo/service/src/main/Q_both.java"
rm -rf "$repo/service/target"
cnt13="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out13b="$(cd "$repo" && MVNW_TMPFS_RC=137 MVNW_FAIL=1 NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc13b=$?
[[ $rc13b -ne 0 && "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((cnt13 + 2))" && "$(printf '%s\n' "$out13b" | grep -c 'retrying once')" == 1 ]] && ok "a disk build that also fails ends the run: one retry, no loop" || bad "double failure (rc $rc13b): $out13b"
echo 'class Q_plain {}' > "$repo/service/src/main/Q_plain.java"
cnt13="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out13c="$(cd "$repo" && MVNW_FAIL=1 NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc13c=$?
[[ $rc13c -ne 0 && "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((cnt13 + 1))" && "$out13c" != *"retrying"* ]] && ok "an ordinary failure (rc 1) is not retried" || bad "ordinary failure retried (rc $rc13c): $out13c"
rm -f "$repo/service/src/main"/Q*.java

echo "Test 14 (nexus-0a1hw review): a tmpfs build that ran the jOOQ codegen says so; an explicit mode warns where auto would refuse"
jq14="$WORKDIR/jooq14"
mkdir -p "$jq14/main-old-prod/generated" "$jq14/test-old-prod/generated"
for k in main test; do echo 'class O {}' > "$jq14/$k-old-prod/generated/O.java"; echo digest > "$jq14/$k-old-prod/complete"; done
echo 'class R {}' > "$repo/service/src/main/R.java"
rm -rf "$repo/service/target"
out14="$(cd "$repo" && NX_JOOQ_CACHE="$jq14" MVNW_CODEGEN_ROOT="$jq14" NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc14=$?
[[ $rc14 -eq 0 && "$out14" == *"the jOOQ codegen ran in this tmpfs build"* && "$out14" == *"main-newhash-prod"* ]] && ok "a new jOOQ cache entry is reported as a codegen" || bad "no codegen line (rc $rc14): $out14"
[[ "$out14" != *"warning:"* ]] && ok "a cache that holds entries raises no warning" || bad "unexpected warning: $out14"
echo 'class R2 {}' > "$repo/service/src/main/R2.java"
rm -rf "$repo/service/target"
out14b="$(cd "$repo" && NX_JOOQ_CACHE="$jq14" NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"
[[ "$out14b" != *"the jOOQ codegen ran"* ]] && ok "no codegen line when the cache did not change" || bad "false codegen line: $out14b"
echo 'class R3 {}' > "$repo/service/src/main/R3.java"
rm -rf "$repo/service/target"
out14c="$(cd "$repo" && NX_JOOQ_CACHE="$WORKDIR/jooq14-missing" NX_BUILD_TMPFS="$shm" scripts/build-gate-jar.sh 2>&1)"; rc14c=$?
[[ $rc14c -eq 0 && "$out14c" == *"warning: building in tmpfs although"*"jOOQ cache"* && "$(last_builddir)" == "$shm/"* ]] && ok "explicit NX_BUILD_TMPFS=<dir> with no usable cache: tmpfs, with a warning" || bad "explicit warning (rc $rc14c): $out14c"
rm -f "$repo/service/src/main/R.java" "$repo/service/src/main/R2.java" "$repo/service/src/main/R3.java"

echo "Test 15 (nexus-0a1hw review): auto inside a grant takes tmpfs only when the grant limit is readable and the jOOQ cache can serve"
okcg="$WORKDIR/okcg"; mkdir -p "$okcg/grant"
echo max > "$okcg/memory.max"; echo 1 > "$okcg/memory.current"
echo 107374182400 > "$okcg/grant/memory.max"; echo 1048576 > "$okcg/grant/memory.current"
printf 'anon 0\nactive_file 0\ninactive_file 0\n' > "$okcg/grant/memory.stat"
auto() {  # <NX_JOOQ_CACHE> [script args...]
    local jc="$1"; shift
    (cd "$repo" && env BUILD_TMPFS_OS=Linux LAB_CPUS=8 NX_BUILD_LEASE_WAIT=0 BUILD_TMPFS_SHM="$shm" BUILD_TMPFS_PROC_CGROUP='0::/grant' \
        BUILD_TMPFS_CGROUP_ROOT="${CGR:-$okcg}" NX_JOOQ_CACHE="$jc" scripts/build-gate-jar.sh "$@" 2>&1)
}
echo 'class T1 {}' > "$repo/service/src/main/T1.java"; rm -rf "$repo/service/target"
out15="$(auto "$jq14")"
[[ "$out15" == *"gate jar build directory: $shm/"*"(tmpfs;"* ]] && ok "auto: tmpfs when the limit is readable and the cache holds entries" || bad "auto, good: $out15"
echo 'class T2 {}' > "$repo/service/src/main/T2.java"; rm -rf "$repo/service/target"
out15b="$(auto "$WORKDIR/jooq15-empty")"
[[ "$out15b" == *"gate jar build directory: service/target (the jOOQ cache"* && "$(last_builddir)" == "$svc_target" ]] && ok "auto: disk when the jOOQ cache cannot serve" || bad "auto, no cache: $out15b"
echo 'class T3 {}' > "$repo/service/src/main/T3.java"; rm -rf "$repo/service/target"
out15c="$(CGR="$WORKDIR/nocg" auto "$jq14")"
[[ "$out15c" == *"gate jar build directory: service/target (inside a lab-run grant but its memory limit cannot be read"* ]] && ok "auto: disk when the grant's memory limit cannot be read" || bad "auto, no limit: $out15c"
echo 'class T4 {}' > "$repo/service/src/main/T4.java"; rm -rf "$repo/service/target"
out15d="$(auto "$jq14" -Dnexus.jooq.cache.dir=off)"
[[ "$out15d" == *"gate jar build directory: service/target (the jOOQ cache is off"* ]] && ok "auto: disk when the build is given -Dnexus.jooq.cache.dir=off" || bad "auto, -D off: $out15d"
rm -f "$repo/service/src/main/T1.java" "$repo/service/src/main/T2.java" "$repo/service/src/main/T3.java" "$repo/service/src/main/T4.java"

echo "Test 16 (nexus-0a1hw review): a signal to the script stops Maven and its children and leaves no tmpfs directory"
echo 'class U {}' > "$repo/service/src/main/U.java"
for sig16 in TERM HUP; do
    rm -rf "$repo/service/target"
    hp="$WORKDIR/hang.pid.$sig16"; hk="$WORKDIR/hang.kid.$sig16"; rm -f "$hp" "$hk"
    (cd "$repo" && MVNW_HANG=1 MVNW_HANG_PID="$hp" MVNW_HANG_KID="$hk" NX_BUILD_TMPFS="$shm" exec scripts/build-gate-jar.sh >"$WORKDIR/hang.out.$sig16" 2>&1) &
    spid=$!
    for _ in $(seq 1 100); do [[ -s "$hk" ]] && break; sleep 0.1; done
    [[ -s "$hk" && -n "$(ls -A "$shm/nexus-build-$(id -u)" 2>/dev/null)" ]] && ok "[$sig16] the build is running in a tmpfs directory" || bad "[$sig16] stub never started"
    mpid="$(cat "$hp" 2>/dev/null)"; kid="$(cat "$hk" 2>/dev/null)"
    kill "-$sig16" "$spid" 2>/dev/null
    rc16=0; wait "$spid" 2>/dev/null || rc16=$?
    sleep 0.5
    [[ $rc16 -ne 0 ]] && ok "[$sig16] the script exits non-zero ($rc16)" || bad "[$sig16] exited 0"
    [[ -z "$(ls -A "$shm/nexus-build-$(id -u)" 2>/dev/null)" ]] && ok "[$sig16] no tmpfs directory remains (the build did not re-create it)" || bad "[$sig16] tmpfs directory remains (script rc $rc16): $(ls -A "$shm/nexus-build-$(id -u)") / $(cat "$WORKDIR/hang.out.$sig16")"
    { [[ -n "$mpid" ]] && ! kill -0 "$mpid" 2>/dev/null; } && ok "[$sig16] the Maven stand-in is gone" || bad "[$sig16] the Maven stand-in survived (pid $mpid)"
    { [[ -n "$kid" ]] && ! kill -0 "$kid" 2>/dev/null; } && ok "[$sig16] and so is its child" || { bad "[$sig16] the Maven child survived (pid $kid)"; kill -9 "$kid" "$mpid" 2>/dev/null; }
    [[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "[$sig16] release.properties restored" || bad "[$sig16] props dirty"
    [[ ! -d "$repo/.git/nexus-build-lease/service" ]] && ok "[$sig16] the lease is released" || bad "[$sig16] lease still held"
done
rm -f "$repo/service/src/main/U.java"

echo "Test 6: inside a lab-run grant a held lease refuses at once (nexus-dfg0q)"
(cd "$repo" && bash -c "source scripts/lib/build-lease.sh; build_lease_acquire service || exit 9; sleep 15") &
holder6=$!
for _ in $(seq 1 50); do [[ -f "$repo/.git/nexus-build-lease/service/pid" ]] && break; sleep 0.1; done
before6="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
t0=$SECONDS
out7="$(cd "$repo" && LAB_CPUS=8 NX_BUILD_LEASE_WAIT=60 NX_GATE_JAR_CACHE=off scripts/build-gate-jar.sh 2>&1)"; rc7=$?
[[ $rc7 -eq 75 && $((SECONDS - t0)) -lt 15 ]] && ok "refused with rc 75 at once" || bad "rc $rc7 after $((SECONDS - t0)) s: $out7"
[[ "$out7" == *"inside a lab-run grant"* ]] && ok "the refusal says why" || bad "refusal text: $out7"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$before6" ]] && ok "mvnw not invoked" || bad "mvnw ran inside the grant"
kill -9 "$holder6" 2>/dev/null; wait "$holder6" 2>/dev/null
rm -rf "$repo/.git/nexus-build-lease/service"

echo
echo "build-gate-jar_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
