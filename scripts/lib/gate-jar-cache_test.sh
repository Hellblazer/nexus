#!/usr/bin/env bash
# scripts/lib/gate-jar-cache_test.sh — shell tests for gate-jar-cache.sh
# (bead nexus-g6xpa). Self-provisioning: a throwaway git repo with a fake
# service/ tree and a worktree of it; never touches the real checkout, never
# runs Maven. Run directly with bash:
#   bash scripts/lib/gate-jar-cache_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/gate_jar_cache_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

PASS=0; FAIL=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

# shellcheck source=./gate-jar-cache.sh disable=SC1091
source "$HERE/gate-jar-cache.sh"

repo="$WORKDIR/repo"
mkdir -p "$repo/service/src" "$repo/service/target"
git -C "$repo" init -q
git -C "$repo" config user.email t@t; git -C "$repo" config user.name t
printf 'service/target/\nservice/.build-lease/\n' > "$repo/.gitignore"
echo 'class A {}' > "$repo/service/src/A.java"
echo 'junk' > "$repo/service/target/out.class"
git -C "$repo" add .gitignore service && git -C "$repo" commit -qm base

_fake_jar() {  # a real zip with a manifest, so the completeness probe passes
    python3 - "$1" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as zf:
    zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    zf.writestr("payload", sys.argv[1])
PY
}

echo "Test 1: key is stable, ignores gitignored target/, tracks content"
k1="$(gate_jar_cache_key "$repo" 0.1.0 "")"; rc=$?
if [[ $rc -eq 0 && ${#k1} -eq 40 ]]; then ok "key computed ($k1)"; else bad "key failed rc=$rc: $k1"; fi
k1b="$(gate_jar_cache_key "$repo" 0.1.0 "")"
[[ "$k1" == "$k1b" ]] && ok "key stable across calls" || bad "key unstable: $k1 vs $k1b"
echo 'more junk' > "$repo/service/target/other.class"
k1c="$(gate_jar_cache_key "$repo" 0.1.0 "")"
[[ "$k1" == "$k1c" ]] && ok "gitignored service/target does not move the key" || bad "target/ moved the key"
echo '// edit' >> "$repo/service/src/A.java"
k2="$(gate_jar_cache_key "$repo" 0.1.0 "")"
[[ "$k1" != "$k2" ]] && ok "an uncommitted tracked edit moves the key" || bad "tracked edit did not move the key"
echo 'class B {}' > "$repo/service/src/B.java"
k3="$(gate_jar_cache_key "$repo" 0.1.0 "")"
[[ "$k2" != "$k3" ]] && ok "an untracked new file moves the key" || bad "untracked file did not move the key"
git -C "$repo" status --porcelain | grep -q 'service/src/B.java' && ok "the real index is untouched (B.java still untracked)" || bad "the real index was modified"
k4="$(gate_jar_cache_key "$repo" 0.2.0 "")"
[[ "$k3" != "$k4" ]] && ok "release_version is part of the key" || bad "release_version not in key"
k5="$(gate_jar_cache_key "$repo" 0.1.0 "-Pnative")"
[[ "$k3" != "$k5" ]] && ok "mvn args are part of the key" || bad "args not in key"
echo 'x' > "$repo/outside.txt"
k6="$(gate_jar_cache_key "$repo" 0.1.0 "")"
[[ "$k3" == "$k6" ]] && ok "a file outside service/ does not move the key" || bad "outside file moved the key"

echo "Test 2: store/lookup roundtrip, worktree shares the cache, corrupt entry is a miss"
jar="$WORKDIR/built.jar"; _fake_jar "$jar"
gate_jar_cache_store "$repo" "$k3" "$jar" "abc+1" && ok "store rc 0" || bad "store failed"
hit="$(gate_jar_cache_lookup "$repo" "$k3")" && ok "lookup hit: $hit" || bad "lookup missed after store"
[[ "$(cat "$hit/build_ref")" == "abc+1" ]] && ok "build_ref round-trips" || bad "build_ref lost"
cmp -s "$jar" "$hit/jar" && ok "jar bytes identical" || bad "jar bytes differ"
gate_jar_cache_lookup "$repo" "nope" >/dev/null 2>&1 && bad "unknown key hit" || ok "unknown key is a miss"
root="$(gate_jar_cache_root "$repo")"
[[ "$(cd "$root" && pwd -P)" == "$(cd "$repo/.git" && pwd -P)/nexus-gate-jar-cache" ]] && ok "root is in the git common dir" || bad "root is $root"
git -C "$repo" worktree add -q "$WORKDIR/wt" -b wt 2>/dev/null
cp "$repo/service/src/B.java" "$WORKDIR/wt/service/src/B.java"
# the worktree branch lacks the uncommitted A.java edit; replay it so content matches
cp "$repo/service/src/A.java" "$WORKDIR/wt/service/src/A.java"
kw="$(gate_jar_cache_key "$WORKDIR/wt" 0.1.0 "")"
[[ "$kw" == "$k3" ]] && ok "worktree with identical service/ content computes the same key" || bad "worktree key differs: $kw vs $k3"
gate_jar_cache_lookup "$WORKDIR/wt" "$k3" >/dev/null && ok "worktree hits the primary's cache entry" || bad "worktree missed the shared cache"
echo 'not a zip' > "$hit/jar"
gate_jar_cache_lookup "$repo" "$k3" >/dev/null 2>&1 && bad "corrupt entry served" || ok "corrupt entry is a miss"
[[ ! -d "$hit" ]] && ok "corrupt entry removed" || bad "corrupt entry still present"

echo "Test 3: off switch and pruning"
NX_GATE_JAR_CACHE=off gate_jar_cache_lookup "$repo" "$k3" >/dev/null 2>&1 && bad "off still hits" || ok "NX_GATE_JAR_CACHE=off disables lookup"
NX_GATE_JAR_CACHE=off gate_jar_cache_store "$repo" "$k3" "$jar" "x" && ok "off store is a no-op rc 0" || bad "off store failed"
[[ ! -d "$root/$k3" ]] && ok "off store wrote nothing" || bad "off store wrote an entry"
for i in 1 2 3 4 5 6 7; do
    GATE_JAR_CACHE_KEEP=3 gate_jar_cache_store "$repo" "key$i" "$jar" "ref$i"; sleep 1
done
n="$(ls -d "$root"/key* | wc -l | tr -d ' ')"
[[ "$n" -eq 3 ]] && ok "prune keeps the newest 3 (found $n)" || bad "prune kept $n entries"
[[ -d "$root/key7" ]] && ok "newest entry survives prune" || bad "newest entry pruned"

echo
echo "gate-jar-cache_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
