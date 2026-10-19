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
cp "$HERE/lib/build-lease.sh" "$HERE/lib/release-props-lease.sh" "$HERE/lib/gate-jar-cache.sh" "$repo/scripts/lib/"
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
mkdir -p target
python3 - "$PWD/target/nexus-service-1.0-SNAPSHOT.jar" "$PWD/src/main/resources/META-INF/nexus/release.properties" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as zf:
    zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    zf.write(sys.argv[2], "release.properties")
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
p = zipfile.ZipFile(sys.argv[1]).read("release.properties").decode()
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
if [[ "$repo/service/target/nexus-service-1.0-SNAPSHOT.jar" -nt "$repo/service/src/main/A.java" ]]; then ok "copied jar is newer than the sources (freshness gate satisfied)"; else bad "copied jar older than sources"; fi
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
