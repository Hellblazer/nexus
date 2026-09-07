#!/usr/bin/env bash
# scripts/build-gate-jar_test.sh — script-level test for build-gate-jar.sh
# (bead nexus-g6xpa): a throwaway git repo with a FAKE service/mvnw that
# writes a real (zip) jar, so the miss → build → store → hit path and the
# failure → restore path run end to end without Maven or Docker. Run with:
#   bash scripts/build-gate-jar_test.sh
set -u -o pipefail

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
cp "$HERE/lib/build-lease.sh" "$HERE/lib/gate-jar-cache.sh" "$repo/scripts/lib/"
chmod +x "$repo/scripts/build-gate-jar.sh"
printf 'release_version=\nbuild_ref=\nname=nexus\n' > "$repo/$props_rel"
echo 'REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 107)' > "$repo/src/nexus/engine_version.py"
echo 'class A {}' > "$repo/service/A.java"
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
if [[ "$repo/service/target/nexus-service-1.0-SNAPSHOT.jar" -nt "$repo/service/A.java" ]]; then ok "copied jar is newer than the sources (freshness gate satisfied)"; else bad "copied jar older than sources"; fi
[[ "$out3" == *"built "* ]] && ok "hit prints the built line callers grep for" || bad "no built line on hit"

echo "Test 4: a leftover stamp in release.properties does not defeat the key"
printf 'release_version=9.9.9\nbuild_ref=stale+1-2\nname=nexus\n' > "$repo/$props_rel"
out4="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc4=$?
[[ $rc4 -eq 0 && "$out4" == *"HIT"* ]] && ok "still a HIT with a polluted stamp (key normalized)" || bad "polluted stamp changed the key: $out4"
git -C "$repo" checkout -q -- "$props_rel"

echo "Test 5: a service/ change misses and rebuilds; NX_GATE_JAR_CACHE=off always builds"
echo 'class B {}' > "$repo/service/B.java"
out5="$(cd "$repo" && scripts/build-gate-jar.sh 2>&1)"; rc5=$?
[[ $rc5 -eq 0 && "$out5" == *"MISS"* ]] && ok "changed service/ misses" || bad "expected MISS: $out5"
[[ "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == 3 ]] && ok "mvnw rebuilt" || bad "mvnw count $(wc -l < "$MVNW_COUNT")"
out6="$(cd "$repo" && NX_GATE_JAR_CACHE=off scripts/build-gate-jar.sh 2>&1)"; rc6=$?
[[ $rc6 -eq 0 && "$out6" == *"MISS"* && "$(wc -l < "$MVNW_COUNT" | tr -d ' ')" == 4 ]] && ok "cache off: builds every time" || bad "cache off misbehaved: $out6"

echo
echo "build-gate-jar_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
