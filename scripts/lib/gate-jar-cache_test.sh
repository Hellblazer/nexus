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
mkdir -p "$repo/service/src/main" "$repo/service/src/test" "$repo/service/.mvn" "$repo/service/target"
git -C "$repo" init -q
git -C "$repo" config user.email t@t; git -C "$repo" config user.name t
printf 'service/target/\nservice/.build-lease/\n' > "$repo/.gitignore"
echo 'class A {}' > "$repo/service/src/main/A.java"
echo 'class T {}' > "$repo/service/src/test/T.java"
echo '<project/>' > "$repo/service/pom.xml"
echo '#!/bin/sh' > "$repo/service/mvnw"
echo '-Xmx1g' > "$repo/service/.mvn/jvm.config"
echo 'readme' > "$repo/service/README.md"
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
echo '// edit' >> "$repo/service/src/main/A.java"
k2="$(gate_jar_cache_key "$repo" 0.1.0 "")"
[[ "$k1" != "$k2" ]] && ok "an uncommitted tracked edit moves the key" || bad "tracked edit did not move the key"
echo 'class B {}' > "$repo/service/src/main/B.java"
k3="$(gate_jar_cache_key "$repo" 0.1.0 "")"
[[ "$k2" != "$k3" ]] && ok "an untracked new file moves the key" || bad "untracked file did not move the key"
status_after="$(git -C "$repo" status --porcelain)"
[[ "$status_after" == *"?? service/src/main/B.java"* ]] && ok "the real index is untouched (B.java still untracked)" || bad "the real index was modified: $status_after"
k4="$(gate_jar_cache_key "$repo" 0.2.0 "")"
[[ "$k3" != "$k4" ]] && ok "release_version is part of the key" || bad "release_version not in key"
k5="$(gate_jar_cache_key "$repo" 0.1.0 "-Pnative")"
[[ "$k3" != "$k5" ]] && ok "mvn args are part of the key" || bad "args not in key"
echo 'x' > "$repo/outside.txt"
k6="$(gate_jar_cache_key "$repo" 0.1.0 "")"
[[ "$k3" == "$k6" ]] && ok "a file outside service/ does not move the key" || bad "outside file moved the key"

echo "Test 1b (nexus-fkhd3): the key covers what the jar is built from and nothing else"
# A fresh clean fixture so the earlier edits do not blur the comparison.
repo2="$WORKDIR/repo2"
mkdir -p "$repo2/service/src/main" "$repo2/service/src/test" "$repo2/service/.mvn" "$repo2/service/deploy"
git -C "$repo2" init -q
git -C "$repo2" config user.email t@t; git -C "$repo2" config user.name t
echo 'class A {}' > "$repo2/service/src/main/A.java"
echo 'class T {}' > "$repo2/service/src/test/T.java"
echo '<project/>' > "$repo2/service/pom.xml"
echo '#!/bin/sh' > "$repo2/service/mvnw"
echo '-Xmx1g' > "$repo2/service/.mvn/jvm.config"
echo 'readme' > "$repo2/service/README.md"
git -C "$repo2" add service && git -C "$repo2" commit -qm base
kb="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
echo '// test-only edit' >> "$repo2/service/src/test/T.java"
echo 'class T2 {}' > "$repo2/service/src/test/T2.java"
echo 'more readme' >> "$repo2/service/README.md"
echo 'x' > "$repo2/service/deploy/d.sh"
kt="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
[[ -n "$kb" && "$kb" == "$kt" ]] && ok "src/test edits, a new test file, README and deploy/ do NOT move the key" || bad "a non-build input moved the key ($kb vs $kt)"
git -C "$repo2" add service/src/test service/README.md service/deploy
git -C "$repo2" commit -qm "test-only commit"
kc="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
[[ "$kb" == "$kc" ]] && ok "a test-only COMMIT keeps the key" || bad "test-only commit moved the key"
echo '// main edit' >> "$repo2/service/src/main/A.java"
km="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
[[ "$kb" != "$km" ]] && ok "a src/main edit moves the key" || bad "src/main edit did not move the key"
git -C "$repo2" checkout -q -- service/src/main/A.java
echo '<project>x</project>' > "$repo2/service/pom.xml"
kp="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
[[ "$kb" != "$kp" ]] && ok "a pom.xml edit moves the key" || bad "pom edit did not move the key"
git -C "$repo2" checkout -q -- service/pom.xml
echo '-Xmx2g' > "$repo2/service/.mvn/jvm.config"
kj="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
[[ "$kb" != "$kj" ]] && ok "a .mvn/ edit moves the key" || bad ".mvn edit did not move the key"
git -C "$repo2" checkout -q -- service/.mvn/jvm.config
echo '# changed' >> "$repo2/service/mvnw"
kx="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
[[ "$kb" != "$kx" ]] && ok "an mvnw edit moves the key" || bad "mvnw edit did not move the key"
git -C "$repo2" checkout -q -- service/mvnw
rm -rf "$repo2/service/.mvn"
kg="$(gate_jar_cache_key "$repo2" 0.1.0 "")"; rcg=$?
[[ $rcg -eq 0 && -n "$kg" && "$kb" != "$kg" ]] && ok "a tracked key dir removed from the working tree still keys (and differs)" || bad "removed .mvn/: rc=$rcg key=$kg"
git -C "$repo2" checkout -q -- service/.mvn
echo 'class N {}' > "$repo2/service/src/main/N.java"
kn="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
[[ "$kb" != "$kn" ]] && ok "a new untracked src/main file moves the key" || bad "new main file did not move the key"
rm "$repo2/service/src/main/N.java" "$repo2/service/src/main/A.java"
kd="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
[[ "$kb" != "$kd" ]] && ok "deleting a src/main file moves the key" || bad "deleting a main file did not move the key"
git -C "$repo2" checkout -q -- service/src/main/A.java
kr="$(gate_jar_cache_key "$repo2" 0.1.0 "")"
[[ "$kb" == "$kr" ]] && ok "restoring the inputs restores the key" || bad "key did not return after restore"

echo "Test 1c (nexus-fkhd3 review): a path with a space is part of the key"
repo3="$WORKDIR/repo3"
mkdir -p "$repo3/service/src/main/dir one" "$repo3/service/.mvn"
git -C "$repo3" init -q
git -C "$repo3" config user.email t@t; git -C "$repo3" config user.name t
echo 'class A {}' > "$repo3/service/src/main/dir one/A.java"
echo '<project/>' > "$repo3/service/pom.xml"
echo '#!/bin/sh' > "$repo3/service/mvnw"
git -C "$repo3" add service && git -C "$repo3" commit -qm base
ks1="$(gate_jar_cache_key "$repo3" 0.1.0 "")"
mv "$repo3/service/src/main/dir one" "$repo3/service/src/main/dir two"
ks2="$(gate_jar_cache_key "$repo3" 0.1.0 "")"
[[ -n "$ks1" && "$ks1" != "$ks2" ]] && ok "renaming a directory whose name has a space moves the key" || bad "space-path rename did not move the key ($ks1 vs $ks2)"

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
cp "$repo/service/src/main/B.java" "$WORKDIR/wt/service/src/main/B.java"
# the worktree branch lacks the uncommitted A.java edit; replay it so content matches
cp "$repo/service/src/main/A.java" "$WORKDIR/wt/service/src/main/A.java"
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

echo "Test 4 (nexus-fkhd3): the jOOQ generated-tree cache is trimmed per kind, newest used first"
jroot="$WORKDIR/jooq-cache"
mkdir -p "$jroot"
for i in 1 2 3 4 5; do
    for kind in main test; do
        mkdir -p "$jroot/$kind-h$i/generated"; echo x > "$jroot/$kind-h$i/complete"
        touch -t "20260101010$i" "$jroot/$kind-h$i"
    done
done
mkdir -p "$jroot/.staging.main.dead.wt.1" "$jroot/.staging.main.fresh.wt.2"
touch -t 202601010101 "$jroot/.staging.main.dead.wt.1"
touch -t 202601010109 "$jroot/main-h1" # most recently USED: a restore touches the entry
NX_JOOQ_CACHE="$jroot" JOOQ_CACHE_KEEP=2 gate_jar_cache_prune_jooq "$repo"
left="$(cd "$jroot" && ls -d main-* test-* | tr '\n' ' ')"
# main keeps h1 (touched) and h5 (newest); test keeps h5, h4
[[ "$left" == "main-h1 main-h5 test-h4 test-h5 " ]] && ok "keeps the newest 2 of each kind by use ($left)" || bad "pruned to: $left"
[[ ! -d "$jroot/.staging.main.dead.wt.1" ]] && ok "a day-old staging leftover is removed" || bad "dead staging dir survived"
NX_JOOQ_CACHE=off gate_jar_cache_prune_jooq "$repo" && ok "NX_JOOQ_CACHE=off prunes nothing and succeeds" || bad "off prune failed"
NX_JOOQ_CACHE="$WORKDIR/does-not-exist" gate_jar_cache_prune_jooq "$repo" && ok "a missing cache dir is a no-op" || bad "missing dir failed"

echo "Test 5 (nexus-fkhd3 review): a cache that cannot be written never fails the caller"
afile="$WORKDIR/a-regular-file"; echo x > "$afile"
NX_GATE_JAR_CACHE="$afile/cache" gate_jar_cache_store "$repo" "$k3" "$jar" "x" 2>"$WORKDIR/store.err" \
    && ok "store under a path that cannot be created returns 0" || bad "unwritable store returned non-zero"
grep -q "gate jar cache" "$WORKDIR/store.err" && ok "the failure is warned about, not silent" || bad "no warning on an unwritable store"
NX_GATE_JAR_CACHE="$afile/cache" gate_jar_cache_lookup "$repo" "$k3" >/dev/null 2>&1 && bad "lookup hit in an unusable cache" || ok "lookup in an unusable cache is a miss"
if [[ "$(id -u)" != 0 ]]; then
    ro="$WORKDIR/ro-cache"; mkdir -p "$ro"; chmod 555 "$ro"
    NX_GATE_JAR_CACHE="$ro" gate_jar_cache_store "$repo" "$k3" "$jar" "x" 2>/dev/null \
        && ok "store into a read-only cache dir returns 0" || bad "read-only store returned non-zero"
    chmod 755 "$ro"
    [[ -z "$(ls -A "$ro")" ]] && ok "the read-only cache holds nothing" || bad "read-only cache was written"
else
    ok "(root: the read-only variant is covered by the regular-file variant above)"
fi
gate_jar_cache_store "$repo" "$k3" "$jar.missing" "x" 2>/dev/null && bad "store without a jar returned 0" || ok "store without a jar is still an error (a caller bug, not a cache fault)"

echo "Test 6 (nexus-fkhd3 review): prune renames an entry aside before it deletes it"
# A restore that is copying an entry while a prune runs must see the entry vanish by RENAME,
# never a tree being emptied under it: record what every rm is asked to remove.
rm_log="$WORKDIR/rm.log"; : > "$rm_log"
rm() {
    local a
    for a in "$@"; do
        [[ "$a" == -* ]] && continue
        printf '%s\n' "$a" >> "$rm_log"
    done
    command rm "$@"
}
proot="$WORKDIR/prune-root"; mkdir -p "$proot"
for i in 1 2 3 4; do mkdir -p "$proot/k$i"; echo x > "$proot/k$i/f"; touch -t "20260101010$i" "$proot/k$i"; done
GATE_JAR_CACHE_KEEP=2 _gate_jar_cache_prune "$proot"
[[ -d "$proot/k4" && -d "$proot/k3" && ! -e "$proot/k1" && ! -e "$proot/k2" ]] && ok "gate-jar prune kept the newest 2" || bad "gate-jar prune result: $(ls "$proot")"
if grep -E "/k[0-9]+$" "$rm_log" >/dev/null; then bad "gate-jar prune deleted an entry in place: $(grep -E '/k[0-9]+$' "$rm_log" | head -2)"; else ok "gate-jar prune never rm'd an entry under its own name"; fi
[[ -z "$(ls -A "$proot" | grep -E '\.old')" ]] && ok "no .old leftovers after the prune" || bad "leftovers: $(ls -A "$proot")"
: > "$rm_log"
jr2="$WORKDIR/jooq-prune"; mkdir -p "$jr2"
for i in 1 2 3; do mkdir -p "$jr2/main-h$i/generated"; touch -t "20260101010$i" "$jr2/main-h$i"; done
NX_JOOQ_CACHE="$jr2" JOOQ_CACHE_KEEP=1 gate_jar_cache_prune_jooq "$repo"
[[ -d "$jr2/main-h3" && ! -e "$jr2/main-h1" && ! -e "$jr2/main-h2" ]] && ok "jooq prune kept the newest 1" || bad "jooq prune result: $(ls "$jr2")"
if grep -E "/main-h[0-9]+$" "$rm_log" >/dev/null; then bad "jooq prune deleted an entry in place"; else ok "jooq prune never rm'd an entry under its own name"; fi
unset -f rm

echo
echo "gate-jar-cache_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
