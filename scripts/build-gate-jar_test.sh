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
# Never the real per-host lease-events log (nexus-jqk5c).
export NX_LEASE_EVENTS_LOG="$WORKDIR/lease-events.log"
PASS=0; FAIL=0; SKIP=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  [skip] $1"; SKIP=$((SKIP + 1)); }
# A signal ignored on entry to the shell cannot be trapped, by this script's children either: nohup
# ignores HUP, and a job launched in the background of a non-interactive shell ignores INT and QUIT.
# That is a property of how the suite was launched, not of the scripts under test, so the signal cases
# skip (loudly, counted) there; a foreground run exercises them.
_trap_works() {  # _trap_works <SIG>
    bash -c "trap 'exit 7' $1; kill -s $1 \$\$; sleep 1; exit 0" >/dev/null 2>&1
    [[ $? -eq 7 ]]
}

repo="$WORKDIR/repo"
props_rel="service/src/main/resources/META-INF/nexus/release.properties"
mkdir -p "$repo/scripts/lib" "$repo/service/src/main/resources/META-INF/nexus" "$repo/src/nexus"
cp "$HERE/build-gate-jar.sh" "$repo/scripts/"
cp "$HERE/lib/build-lease.sh" "$HERE/lib/release-props-lease.sh" "$HERE/lib/gate-jar-cache.sh" "$HERE/lib/leased-build.sh" "$repo/scripts/lib/"
chmod +x "$repo/scripts/build-gate-jar.sh"
printf 'release_version=\nbuild_ref=\nname=nexus\n' > "$repo/$props_rel"
echo 'REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 107)' > "$repo/src/nexus/engine_version.py"
echo 'class A {}' > "$repo/service/src/main/A.java"
printf 'service/target/\nservice/.build-lease/\n' > "$repo/.gitignore"
# Fake mvnw: MVNW_FAIL=1 fails; otherwise writes a real jar whose payload
# is the stamped release.properties it saw, and counts its invocations.
cat > "$repo/service/mvnw" <<'STUB'
#!/usr/bin/env bash
echo run >> "$MVNW_COUNT"
[[ "${MVNW_FAIL:-0}" == 1 ]] && exit 1
# MVNW_HANG=<file>: publish this pid there and block, as a long build does; a TERM
# is recorded in <file>.term so a test can tell it from a KILL escalation.
# MVNW_IGNORE_TERM=1: the build ignores TERM/HUP and keeps running ~3 s, as a JVM finishing a write does,
# so the wrapper has to wait for the group (a stub that dies at once proves nothing about the wait).
# MVNW_LINGER=<seconds>: leave a background process in the build's group that outlives the build.
if [[ -n "${MVNW_LINGER:-}" ]]; then (sleep "$MVNW_LINGER" >/dev/null 2>&1 &); fi
if [[ -n "${MVNW_HANG:-}" ]]; then
    if [[ "${MVNW_IGNORE_TERM:-0}" == 1 ]]; then
        trap '' TERM HUP QUIT
        echo "$$" > "$MVNW_HANG"; sleep 3
        exit 0
    fi
    # The trap also kills its own `sleep`: a background command of a non-job-control shell ignores
    # SIGQUIT, so without it the group would outlive a QUIT by the full 30 s.
    trap 'echo term > "$MVNW_HANG.term"; kill "$sl" 2>/dev/null; exit 143' TERM HUP QUIT
    echo "$$" > "$MVNW_HANG"; sleep 30 & sl=$!; wait $sl
    exit 0
fi
mkdir -p target
# Worst case for a hard-linked jar: a writer that rewrites the existing inode in
# place and does not respect its read-only mode (root, or a plugin that chmods).
chmod u+w target/nexus-service-1.0-SNAPSHOT.jar 2>/dev/null || true
python3 - "$PWD/target/nexus-service-1.0-SNAPSHOT.jar" "$PWD/src/main/resources/META-INF/nexus/release.properties" <<'PY'
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

# ── Tests 11a-11h (nexus-t6t9f): --lab-run <class> takes the lease FIRST, then builds under
# lab-run as its own process group, mirroring mvnw-leased.sh. A stub lab-run records its args,
# whether the lease was already held when it started, and runs the command it was given.
echo "Test 11a: --lab-run <class> acquires the lease, then runs the build under lab-run, stamped and restored"
leasedir="$repo/.git/nexus-build-lease/service"
labbin="$WORKDIR/labbin"; mkdir -p "$labbin"
cat > "$labbin/lab-run" <<STUB
#!/usr/bin/env bash
{
    echo "LABRUN_ARGS=\$*"
    if [[ -f "$leasedir/pid" ]]; then echo "LEASE_HELD_AT_START=yes"; else echo "LEASE_HELD_AT_START=no"; fi
} > "\$LABRUN_STUB_MARKER"
[[ "\$1" == "-c" && "\$3" == "--" ]] || exit 64
shift 3
exec "\$@"
STUB
chmod +x "$labbin/lab-run"
echo 'class L1 {}' > "$repo/service/src/main/L1.java"
labmark="$WORKDIR/labrun-11a.txt"; rm -f "$labmark"
cnt11="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out11a="$(cd "$repo" && PATH="$labbin:$PATH" LABRUN_STUB_MARKER="$labmark" scripts/build-gate-jar.sh --lab-run lab-m -Pfoo 2>&1)"; rc11a=$?
[[ $rc11a -eq 0 && "$out11a" == *"MISS"* && "$out11a" == *"built "* ]] && ok "the --lab-run build succeeded" || bad "rc $rc11a: $out11a"
labc="$(cat "$labmark" 2>/dev/null)"
[[ "$labc" == "LABRUN_ARGS=-c lab-m -- python3 -c "*" ./mvnw -q package -DskipTests -Dnexus.jlink.skip=true -Pfoo"* ]] && ok "lab-run got the class and ./mvnw with the mvn args untouched (--lab-run itself consumed)" || bad "lab-run args: ${labc:-<never invoked>}"
[[ "$labc" == *"LEASE_HELD_AT_START=yes"* ]] && ok "the lease was already held when lab-run started (lease before grant)" || bad "lab-run started without the lease: $labc"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$((cnt11 + 1))" ]] && ok "mvnw ran once, inside lab-run" || bad "mvnw count $(wc -l < "$MVNW_COUNT")"
ref11="$(printf '%s\n' "$out11a" | sed -n 's/^stamped build_ref=//p')"
python3 - "$tjar" "$ref11" <<'PY' && ok "the jar carries the stamp and the printed build_ref" || bad "jar lacks the stamp"
import sys, zipfile
p = zipfile.ZipFile(sys.argv[1]).read("META-INF/nexus/release.properties").decode()
assert "release_version=0.1.107\n" in p and f"build_ref={sys.argv[2]}\n" in p, p
PY
[[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "release.properties restored" || bad "props dirty after the --lab-run build"
[[ ! -d "$leasedir" ]] && ok "lease released after the --lab-run build" || bad "lease still held"

echo "Test 11b: a cache HIT under --lab-run never starts lab-run (no grant is taken for a jar that is already built)"
labmark="$WORKDIR/labrun-11b.txt"; rm -f "$labmark"
out11b="$(cd "$repo" && PATH="$labbin:$PATH" LABRUN_STUB_MARKER="$labmark" scripts/build-gate-jar.sh --lab-run lab-m -Pfoo 2>&1)"; rc11b=$?
[[ $rc11b -eq 0 && "$out11b" == *"HIT"* && ! -e "$labmark" ]] && ok "HIT, and lab-run was not invoked" || bad "rc $rc11b, marker $(ls "$labmark" 2>&1): $out11b"
[[ ! -d "$leasedir" ]] && ok "lease released after the HIT" || bad "lease still held after the HIT"

echo "Test 11c: --lab-run with no lab-run on PATH exits 127 and takes no lease; without a class it exits 64"
out11c="$(cd "$repo" && PATH="/usr/bin:/bin" scripts/build-gate-jar.sh --lab-run lab-m 2>&1)"; rc11c=$?
[[ $rc11c -eq 127 && "$out11c" == *"lab-run is not on PATH"* && ! -d "$leasedir" ]] && ok "rc 127, no lease left" || bad "rc $rc11c: $out11c"
out11c2="$(cd "$repo" && PATH="$labbin:$PATH" scripts/build-gate-jar.sh --lab-run 2>&1)"; rc11c2=$?
[[ $rc11c2 -eq 64 && ! -d "$leasedir" ]] && ok "rc 64 for --lab-run with no class" || bad "rc $rc11c2: $out11c2"

echo "Test 11d: --lab-run inside a lab-run grant is refused (no nested grant), before any lease or build"
cnt11d="$(wc -l < "$MVNW_COUNT" | tr -d ' ')"
out11d="$(cd "$repo" && LAB_CPUS=8 PATH="$labbin:$PATH" scripts/build-gate-jar.sh --lab-run lab-m 2>&1)"; rc11d=$?
[[ $rc11d -eq 64 && "$out11d" == *"inside a lab-run grant"* ]] && ok "rc 64 naming the nested grant" || bad "rc $rc11d: $out11d"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == "$cnt11d" && ! -d "$leasedir" ]] && ok "no build, no lease left" || bad "a build ran or the lease was left"

echo "Test 11e: a failing build under --lab-run propagates its exit code, restores release.properties, releases the lease"
echo 'class L2 {}' > "$repo/service/src/main/L2.java"
out11e="$(cd "$repo" && PATH="$labbin:$PATH" LABRUN_STUB_MARKER="$WORKDIR/labrun-11e.txt" MVNW_FAIL=1 scripts/build-gate-jar.sh --lab-run lab-m 2>&1)"; rc11e=$?
[[ $rc11e -eq 1 ]] && ok "exit 1 from mvnw comes back as 1" || bad "rc $rc11e: $out11e"
[[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "release.properties restored" || bad "props dirty after a failed --lab-run build"
[[ ! -d "$leasedir" ]] && ok "lease released after the failed build" || bad "lease left held after a failed --lab-run build"

echo "Test 11f: inside a grant, the refusal names the --lab-run form"
(cd "$repo" && bash -c "source scripts/lib/build-lease.sh; build_lease_acquire service || exit 9; sleep 15") &
holder11f=$!
for _ in $(seq 1 50); do [[ -f "$leasedir/pid" ]] && break; sleep 0.1; done
out11f="$(cd "$repo" && LAB_CPUS=8 NX_GATE_JAR_CACHE=off scripts/build-gate-jar.sh 2>&1)"; rc11f=$?
[[ $rc11f -eq 75 && "$out11f" == *"--lab-run <class>"* ]] && ok "the refusal names the --lab-run form" || bad "rc $rc11f: $out11f"
kill -9 "$holder11f" 2>/dev/null; wait "$holder11f" 2>/dev/null
rm -rf "$leasedir"

# TERM while a build runs. The stub lab-run below dies on TERM and leaves its command running (what sudo
# does on chas), the worst case: the wrapper must signal the build's own group, and release.properties
# and the lease must go back only after that group is gone.
_term_case() {  # _term_case <label> <extra build-gate-jar.sh args...>; env TERM_SIG (default TERM), IGNORE_TERM (default 0)
    local label="$1"; shift
    local sig="${TERM_SIG:-TERM}" ignore="${IGNORE_TERM:-0}"
    if ! _trap_works "$sig"; then
        skip "[$label] $sig is ignored on entry in this launch environment (nohup, or a backgrounded non-interactive suite): it cannot be trapped, so there is nothing to forward; run the suite in the foreground"
        return 0
    fi
    local started="$WORKDIR/term-$label.started" build wrapper script_pid term_start released_while_alive=0
    rm -f "$started" "$started.term"
    set -m
    (cd "$repo" && PATH="$labbin:$PATH" LABRUN_STUB_MARKER="$WORKDIR/labrun-$label.txt" MVNW_HANG="$started" \
        NX_GATE_JAR_CACHE=off MVNW_IGNORE_TERM="$ignore" scripts/build-gate-jar.sh "$@" >"$WORKDIR/term-$label.out" 2>&1) &
    wrapper=$!
    set +m
    for _ in $(seq 1 100); do [[ -f "$started" ]] && break; sleep 0.1; done
    if [[ ! -f "$started" ]]; then
        bad "[$label] the hanging build never started (setup failure)"
        kill -9 "$wrapper" 2>/dev/null; return 0
    fi
    build="$(cat "$started")"
    for _ in $(seq 1 30); do [[ "$(cat "$leasedir/pgid" 2>/dev/null)" == "$build" ]] && break; sleep 0.1; done
    [[ "$(cat "$leasedir/pgid" 2>/dev/null)" == "$build" ]] && ok "[$label] the lease's pgid names the build's own group" || bad "[$label] the lease's pgid is $(cat "$leasedir/pgid" 2>/dev/null), not $build"
    [[ "$(cat "$repo/$props_rel")" == *"release_version=0.1.107"* ]] && ok "[$label] release.properties is stamped while the build runs" || bad "[$label] props not stamped mid-build"
    # The job is a subshell around the script; the lease's pid is the script itself.
    script_pid="$(cat "$leasedir/pid" 2>/dev/null)"
    term_start=$SECONDS
    kill -s "$sig" "$script_pid" 2>/dev/null
    for _ in $(seq 1 400); do
        if kill -0 -- "-$build" 2>/dev/null; then
            [[ ! -d "$leasedir" ]] && released_while_alive=1
            [[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && released_while_alive=1
        fi
        kill -0 "$wrapper" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 -- "-$build" 2>/dev/null; then
        bad "[$label] the build's group is still alive after the script exited"
        kill -9 -- "-$build" 2>/dev/null
    else
        ok "[$label] the build's group is gone after the script exited"
    fi
    # TERM itself, not a KILL escalation 30 s later: the build recorded it.
    if [[ "$ignore" == 1 ]]; then
        # The build ignored the signal and ended on its own after ~3 s: the wrapper had to wait for it.
        [[ $SECONDS -ge $((term_start + 2)) && $SECONDS -lt $((term_start + 25)) ]] && ok "[$label] the wrapper waited for the build that ignored $sig" || bad "[$label] the wrapper did not wait for a build ignoring $sig ($((SECONDS - term_start)) s)"
    else
        [[ -f "$started.term" && $SECONDS -lt $((term_start + 25)) ]] && ok "[$label] $sig reached the build's own group promptly" || bad "[$label] the build never saw $sig within 25 s (forwarding broken, or only a KILL escalation ended it)"
    fi
    [[ $released_while_alive -eq 0 && ! -d "$leasedir" ]] && ok "[$label] the lease and the props went back only after the build's group was gone" || bad "[$label] lease or props released while the build ran ($released_while_alive) or lease still held after"
    [[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "[$label] release.properties restored after the signal" || bad "[$label] props dirty after the signal: $(cat "$repo/$props_rel")"
    wait "$wrapper" 2>/dev/null
    rm -rf "$leasedir"
}
cat > "$labbin/lab-run" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == "-c" && "$3" == "--" ]] || exit 64
shift 3
trap 'exit 143' TERM HUP QUIT
set -m
"$@" &
set +m
wait $!
STUB
chmod +x "$labbin/lab-run"
echo "Test 11g: TERM under --lab-run reaches the build even when lab-run does not forward it"
_term_case lab --lab-run lab-s
echo "Test 11h: TERM without --lab-run also reaches the build (same signal handling on the direct path)"
_term_case direct
echo "Test 11i: HUP reaches the build and releases only after it is gone (a closed terminal must not leave props stamped and the lease held)"
TERM_SIG=HUP _term_case lab-hup --lab-run lab-s
TERM_SIG=HUP _term_case direct-hup
echo "Test 11j: QUIT is forwarded the same way"
TERM_SIG=QUIT _term_case direct-quit
echo "Test 11k: a build that IGNORES TERM for ~3 s keeps the lease and the stamped props until its group is gone"
IGNORE_TERM=1 _term_case lab-ignore --lab-run lab-s
IGNORE_TERM=1 _term_case direct-ignore

# After the build group exits the wrapper still has work (cache store, props restore). The lease's pgid
# must not keep naming the dead group then, or a waiting peer reclaims it as stale (liveness keys on the
# pgid first) and starts a build over a jar the wrapper is still storing.
echo "Test 11l: a peer cannot reclaim the lease while the wrapper is alive after its build has exited"
_after_build_case() {  # _after_build_case <label> <lab-class-or-empty>
    local label="$1" cls="$2" out="$WORKDIR/after-$1.out" holder peer_out peer_rc
    rm -f "$out"; rm -rf "$leasedir"
    # Stub lab-run from the 11a block would be replaced above; give this case its own pass-through.
    (cd "$repo" && PATH="$labbin:$PATH" bash -c "
        source scripts/lib/build-lease.sh; source scripts/lib/leased-build.sh
        build_lease_acquire service t || exit 9
        leased_build_init t service
        leased_build_run '$cls' sleep 1
        echo BUILT
        sleep 12" >"$out" 2>&1) &
    holder=$!
    for _ in $(seq 1 100); do grep -q BUILT "$out" 2>/dev/null && break; sleep 0.2; done
    if ! grep -q BUILT "$out" 2>/dev/null; then
        bad "[$label] the harness never finished its build (setup failure): $(cat "$out" 2>/dev/null)"
        kill -9 "$holder" 2>/dev/null; wait "$holder" 2>/dev/null; return 0
    fi
    peer_out="$(cd "$repo" && bash -c "source scripts/lib/build-lease.sh; build_lease_acquire service" 2>&1)"; peer_rc=$?
    [[ $peer_rc -eq 75 && "$peer_out" == *"REFUSED"* && "$peer_out" != *"reclaiming"* ]] && ok "[$label] a peer is refused while the wrapper is alive after its build" || bad "[$label] peer rc $peer_rc: $peer_out"
    kill -9 "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
    rm -rf "$leasedir"
}
_after_build_case direct ""
_after_build_case lab lab-s

# A group member that outlives the build leader (a daemon the build started) keeps the lease tracked, and
# the EXIT trap leaves lease and stamp in place with a WARNING rather than release under a live writer.
echo "Test 11m: a process left in the build's group keeps the lease and the stamp until it is gone"
rm -rf "$leasedir"
echo 'class L3 {}' > "$repo/service/src/main/L3.java"
out11m="$(cd "$repo" && MVNW_LINGER=5 NX_GATE_JAR_CACHE=off scripts/build-gate-jar.sh 2>&1)"; rc11m=$?
[[ "$out11m" == *"WARNING"*"exiting WITHOUT releasing"* ]] && ok "the lingering group is named in a WARNING at exit" || bad "rc $rc11m, no WARNING: $out11m"
[[ -d "$leasedir" ]] && ok "the lease is still held while the group is alive" || bad "the lease was released under a live group member"
[[ "$(cat "$repo/$props_rel")" == *"release_version=0.1.107"* ]] && ok "release.properties was left stamped too (both finish steps skipped, as the WARNING says)" || bad "props restored under a live group member"
sleep 6
out11m2="$(cd "$repo" && bash -c "source scripts/lib/build-lease.sh; build_lease_acquire service && build_lease_release service" 2>&1)"; rc11m2=$?
[[ $rc11m2 -eq 0 && "$out11m2" == *"reclaiming stale"* ]] && ok "once the group is gone a later acquire reclaims the lease" || bad "rc $rc11m2: $out11m2"
printf 'release_version=\nbuild_ref=\nname=nexus\n' > "$repo/$props_rel"
rm -rf "$leasedir"

echo "Test 11n: sourcing leased-build.sh before build-lease.sh fails fast, naming the missing library"
out11n="$(bash -c "source '$repo/scripts/lib/leased-build.sh'; echo SOURCED" 2>&1)"; rc11n=$?
[[ $rc11n -ne 0 && "$out11n" == *"source scripts/lib/build-lease.sh first"* && "$out11n" != *SOURCED* ]] && ok "out-of-order source fails fast (rc $rc11n)" || bad "rc $rc11n: $out11n"

echo "Test 12: -h and --help print the usage and exit 0 without a lease, a stamp or a build, in any position"
for help_args in "--help" "-h" "--lab-run lab-m --help" "-Dx=1 -h"; do
    : > "$MVNW_COUNT"
    # shellcheck disable=SC2086 # word splitting is the point: each case is an argv
    out12="$(cd "$repo" && scripts/build-gate-jar.sh $help_args 2>&1)"; rc12=$?
    [[ $rc12 -eq 0 && "$out12" == *"usage: scripts/build-gate-jar.sh"* ]] && ok "'$help_args' prints the usage, rc 0" || bad "'$help_args' rc $rc12: $out12"
    [[ ! -s "$MVNW_COUNT" ]] && ok "'$help_args' ran no build" || bad "'$help_args' invoked mvnw"
    [[ "$(cat "$repo/$props_rel")" == $'release_version=\nbuild_ref=\nname=nexus' ]] && ok "'$help_args' left release.properties alone" || bad "'$help_args' stamped release.properties: $(cat "$repo/$props_rel")"
    [[ ! -e "$repo/service/.build-lease" || -z "$(ls -A "$repo/service/.build-lease" 2>/dev/null)" ]] && ok "'$help_args' took no lease" || bad "'$help_args' left a lease"
done

echo
skipnote=""; (( SKIP == 0 )) || skipnote=", $SKIP skipped"
echo "build-gate-jar_test.sh: $PASS passed, $FAIL failed$skipnote"
[[ $FAIL -eq 0 ]]
