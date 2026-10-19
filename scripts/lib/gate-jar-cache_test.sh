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

_fake_jar() {  # <path> [build_ref]: a real zip with a manifest, so the completeness probe passes;
    # with a build_ref it carries the stamped META-INF/nexus/release.properties a real gate jar has
    python3 - "$1" "${2:-}" "${3:-}" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as zf:
    zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    zf.writestr("payload", sys.argv[3] or sys.argv[1])
    if sys.argv[2]:
        zf.writestr("META-INF/nexus/release.properties", "release_version=0.1.0\nbuild_ref=%s\nname=nexus\n" % sys.argv[2])
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

# Racy-index regression: a same-size edit made in the same second as the commit
# must still move the key. core.checkStat=minimal (seconds + size only) is forced
# for these rounds, the coarse comparison a CI host's git config can carry, and
# the rounds repeat so a hit is near certain: the old plain-cp copy missed ~1 in 20.
racy_misses=0
for i in $(seq 1 40); do
  r3="$WORKDIR/racy$i"; mkdir -p "$r3/service/src/main" "$r3/service/.mvn"
  git -C "$r3" init -q; git -C "$r3" config user.email t@t; git -C "$r3" config user.name t
  echo 'class A {}' > "$r3/service/src/main/A.java"; echo '<project/>' > "$r3/service/pom.xml"
  echo '-Xmx1g' > "$r3/service/.mvn/jvm.config"
  git -C "$r3" add service && git -C "$r3" commit -qm base
  ka="$(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.checkStat GIT_CONFIG_VALUE_0=minimal gate_jar_cache_key "$r3" 0.1.0 "")"
  echo '-Xmx2g' > "$r3/service/.mvn/jvm.config"
  kz="$(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.checkStat GIT_CONFIG_VALUE_0=minimal gate_jar_cache_key "$r3" 0.1.0 "")"
  [[ "$ka" != "$kz" ]] || racy_misses=$((racy_misses + 1))
done
[[ $racy_misses -eq 0 ]] && ok "a same-size edit right after a commit moves the key (40/40 under core.checkStat=minimal, racy-index safe)" || bad "a same-size edit right after a commit kept the key in $racy_misses of 40 rounds (racy index)"
# A same-size edit whose mtime is then RESTORED (touch -r, rsync -t, a cache
# restore) is not racy: only content hashing sees it. Deterministic.
r4="$WORKDIR/restored"; mkdir -p "$r4/service/src/main" "$r4/service/.mvn"
git -C "$r4" init -q; git -C "$r4" config user.email t@t; git -C "$r4" config user.name t
echo 'class A {}' > "$r4/service/src/main/A.java"; echo '<project/>' > "$r4/service/pom.xml"
echo '-Xmx1g' > "$r4/service/.mvn/jvm.config"
# An mtime well before the commit's index write, so the entry is NOT racy.
: > "$r4/ref"
python3 -c 'import os,sys,time; t=time.time()-60; [os.utime(f,(t,t)) for f in sys.argv[1:]]' "$r4/service/.mvn/jvm.config" "$r4/ref"
git -C "$r4" add service && git -C "$r4" commit -qm base
sleep 2
kr0="$(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.checkStat GIT_CONFIG_VALUE_0=minimal gate_jar_cache_key "$r4" 0.1.0 "")"
echo '-Xmx2g' > "$r4/service/.mvn/jvm.config"; touch -r "$r4/ref" "$r4/service/.mvn/jvm.config"
kr1="$(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.checkStat GIT_CONFIG_VALUE_0=minimal gate_jar_cache_key "$r4" 0.1.0 "")"
[[ -n "$kr0" && "$kr0" != "$kr1" ]] && ok "a same-size edit with its old mtime restored moves the key (content, not stat)" || bad "a same-size edit with a restored mtime kept the key (the key trusted cached stat)"
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
jar="$WORKDIR/built.jar"; _fake_jar "$jar" "abc+1"
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
# (a torn write is a NEW inode; the entry's jar is a read-only link shared with built.jar)
rm -f "$hit/jar"; echo 'not a zip' > "$hit/jar"
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
in_place="$(grep -E "/k[0-9]+$" "$rm_log" || true)"
if [[ -n "$in_place" ]]; then bad "gate-jar prune deleted an entry in place: $in_place"; else ok "gate-jar prune never rm'd an entry under its own name"; fi
[[ -z "$(ls -A "$proot" | grep -E '\.old')" ]] && ok "no .old leftovers after the prune" || bad "leftovers: $(ls -A "$proot")"
: > "$rm_log"
jr2="$WORKDIR/jooq-prune"; mkdir -p "$jr2"
for i in 1 2 3; do mkdir -p "$jr2/main-h$i/generated"; touch -t "20260101010$i" "$jr2/main-h$i"; done
NX_JOOQ_CACHE="$jr2" JOOQ_CACHE_KEEP=1 gate_jar_cache_prune_jooq "$repo"
[[ -d "$jr2/main-h3" && ! -e "$jr2/main-h1" && ! -e "$jr2/main-h2" ]] && ok "jooq prune kept the newest 1" || bad "jooq prune result: $(ls "$jr2")"
in_place="$(grep -E "/main-h[0-9]+$" "$rm_log" || true)"
if [[ -n "$in_place" ]]; then bad "jooq prune deleted an entry in place: $in_place"; else ok "jooq prune never rm'd an entry under its own name"; fi
unset -f rm

echo "Test 7 (nexus-k6zxp): a store links the jar into the entry, read-only, and a second store writes nothing"
r7="$WORKDIR/root7"; export NX_GATE_JAR_CACHE="$r7"
j7="$WORKDIR/j7.jar"; _fake_jar "$j7" "ref-7"
gate_jar_cache_store "$repo" k7 "$j7" "ref-7" && ok "store rc 0" || bad "store failed"
[[ "$j7" -ef "$r7/k7/jar" ]] && ok "the entry's jar is the built jar's own inode (no 131 MB copy)" || bad "entry jar is a copy, not a link"
[[ ! -w "$r7/k7/jar" || "$(id -u)" == 0 ]] && ok "the entry's jar is read-only" || bad "entry jar is writable"
[[ "$(cat "$(dirname "$j7")/.gate-jar-key")" == k7 ]] && ok "the key is recorded beside the built jar" || bad "no key marker beside the built jar"
ino7="$(ls -i "$r7/k7/jar" | awk '{print $1}')"
j7b="$WORKDIR/j7b.jar"; _fake_jar "$j7b" "ref-7c" "a different jar"
gate_jar_cache_store "$repo" k7 "$j7b" "ref-7b" && ok "second store rc 0" || bad "second store failed"
[[ "$(ls -i "$r7/k7/jar" | awk '{print $1}')" == "$ino7" && "$(cat "$r7/k7/build_ref")" == "ref-7" ]] \
    && ok "a store of a key whose entry verifies writes nothing" || bad "second store replaced a verified entry"
[[ -z "$(ls -A "$r7" | grep -E '^\.staging|\.old')" ]] && ok "and leaves no staging directory" || bad "leftovers: $(ls -A "$r7")"
printf '' > "$r7/k7/build_ref"
gate_jar_cache_store "$repo" k7 "$j7b" "ref-7c"
[[ "$(cat "$r7/k7/build_ref")" == "ref-7c" ]] && ok "an entry with an EMPTY build_ref does not verify and is repaired by the next store" || bad "empty-build_ref entry survived a store"
NX_GATE_JAR_CACHE="$r7" gate_jar_cache_lookup "$repo" k7 >/dev/null && ok "the repaired entry hits" || bad "repaired entry misses"

echo "Test 7b: store falls back to a copy when linking fails (a different filesystem)"
ln() { return 1; }
j7c="$WORKDIR/j7c.jar"; _fake_jar "$j7c" "ref-7c2"
gate_jar_cache_store "$repo" k7c "$j7c" "ref-7c2" && ok "store rc 0 with ln failing" || bad "store failed without ln"
unset -f ln
[[ -f "$r7/k7c/jar" && ! "$j7c" -ef "$r7/k7c/jar" ]] && cmp -s "$j7c" "$r7/k7c/jar" && ok "the entry holds an independent byte-equal copy" || bad "no copy after a failed link"
[[ -w "$j7c" ]] && ok "the built jar stays writable when it is not shared" || bad "built jar lost its write bit"

echo "Test 8 (nexus-k6zxp): install links, skips when the target already holds the key's jar, and verifies"
t8="$WORKDIR/t8"; mkdir -p "$t8"; dest="$t8/nexus.jar"; entry="$r7/k7"
gate_jar_cache_install k7 "$entry" "$dest" && [[ "$_GATE_JAR_PLACED" == link ]] && ok "first install links" || bad "first install: placed=$_GATE_JAR_PLACED"
[[ "$dest" -ef "$entry/jar" ]] && ok "target is the entry's inode" || bad "target is not the entry's inode"
[[ "$(cat "$t8/.gate-jar-key")" == k7 ]] && ok "key recorded beside the target" || bad "no key marker"
ino8="$(ls -i "$dest" | awk '{print $1}')"
touch -t 202001010000 "$dest"; ref8="$WORKDIR/ref8"; touch -t 202101010000 "$ref8"
touch -t 202001010000 "$t8/.gate-jar-key"
gate_jar_cache_install k7 "$entry" "$dest" && [[ "$_GATE_JAR_PLACED" == kept ]] && ok "second install keeps the target (no copy, no link)" || bad "second install: placed=$_GATE_JAR_PLACED"
[[ "$(ls -i "$dest" | awk '{print $1}')" == "$ino8" ]] && ok "the target's inode is unchanged" || bad "target was replaced"
[[ "$t8/.gate-jar-key" -nt "$ref8" ]] && ok "a kept target's marker mtime is refreshed (the per-worktree freshness signal)" || bad "kept target's marker is stale"
[[ "$ref8" -nt "$dest" ]] && ok "and the jar's own mtime, which a link shares with other worktrees, is NOT touched" || bad "install touched the shared jar's mtime"
# a marker is never trusted alone: a different jar under the same marker is replaced
rm -f "$dest"; cp "$j7" "$dest"; chmod u+w "$dest"
! cmp -s "$dest" "$entry/jar" || bad "test setup: the stand-in jar equals the entry"
gate_jar_cache_install k7 "$entry" "$dest" && [[ "$_GATE_JAR_PLACED" == link ]] && cmp -s "$dest" "$entry/jar" && ok "a jar that differs from the entry is replaced despite the marker" || bad "marker alone was trusted (placed=$_GATE_JAR_PLACED)"
# a byte-equal independent copy under the marker is kept, not rewritten
rm -f "$dest"; cp "$entry/jar" "$dest"; chmod u+w "$dest"
gate_jar_cache_install k7 "$entry" "$dest" && [[ "$_GATE_JAR_PLACED" == kept ]] && ok "a byte-equal copy is kept" || bad "byte-equal copy was rewritten (placed=$_GATE_JAR_PLACED)"
# a marker for another key is not a match
printf 'other\n' > "$t8/.gate-jar-key"
gate_jar_cache_install k7 "$entry" "$dest" && [[ "$_GATE_JAR_PLACED" != kept ]] && ok "a marker naming another key is not a match" || bad "foreign marker matched"
# ln failing: copy fallback is independent and writable
rm -f "$dest" "$t8/.gate-jar-key"
ln() { return 1; }
gate_jar_cache_install k7 "$entry" "$dest"; unset -f ln
[[ "$_GATE_JAR_PLACED" == copy || "$_GATE_JAR_PLACED" == clone || "$_GATE_JAR_PLACED" == clone-or-copy ]] && [[ ! "$dest" -ef "$entry/jar" ]] && cmp -s "$dest" "$entry/jar" && ok "ln failing: an independent byte-equal $_GATE_JAR_PLACED" || bad "no fallback copy (placed=$_GATE_JAR_PLACED)"
[[ -w "$dest" ]] && ok "the fallback copy is writable" || bad "fallback copy is read-only"

echo "Test 8b (nexus-k6zxp): a hit verifies the jar's embedded build_ref against the entry's"
rb="$WORKDIR/rootb"; export NX_GATE_JAR_CACHE="$rb"
jb1="$WORKDIR/jb1.jar"; _fake_jar "$jb1" "ref-b"
gate_jar_cache_store "$repo" kb "$jb1" "ref-b"
gate_jar_cache_lookup "$repo" kb >/dev/null && ok "an entry whose jar carries the entry's build_ref hits" || bad "matching build_ref missed"
# a VALID zip with a manifest, but stamped with another build_ref: what a root/sudo writer leaves
rm -f "$rb/kb/jar"; _fake_jar "$rb/kb/jar" "some-other-ref"
gate_jar_cache_lookup "$repo" kb >/dev/null 2>"$WORKDIR/lookup-b.err" && bad "a jar stamped with a different build_ref was served" || ok "a valid jar stamped with a DIFFERENT build_ref is a miss"
[[ ! -d "$rb/kb" ]] && ok "and the entry is discarded" || bad "mismatching entry still present"
grep -q "build_ref differs" "$WORKDIR/lookup-b.err" && ok "the discard is disclosed" || bad "no disclosure: $(cat "$WORKDIR/lookup-b.err")"
gate_jar_cache_store "$repo" kb "$jb1" "ref-b" && gate_jar_cache_lookup "$repo" kb >/dev/null && ok "the next store repairs it and it hits again" || bad "no repair after the discard"
# a jar with no release.properties at all cannot prove its build_ref: also a miss
rm -f "$rb/kb/jar"; _fake_jar "$rb/kb/jar"
gate_jar_cache_lookup "$repo" kb >/dev/null 2>&1 && bad "a jar with no release.properties was served" || ok "a jar with no embedded build_ref is a miss"
# a store treats such an entry as incomplete too, instead of leaving it in place
gate_jar_cache_store "$repo" kb "$jb1" "ref-b"; rm -f "$rb/kb/jar"; _fake_jar "$rb/kb/jar" "wrong"
gate_jar_cache_store "$repo" kb "$jb1" "ref-b"
gate_jar_cache_lookup "$repo" kb >/dev/null && ok "a store repairs an entry whose jar is stamped with another build_ref" || bad "store left a mismatching entry"
export NX_GATE_JAR_CACHE="$r7"

echo "Test 8c (nexus-k6zxp): the placement label says what happened; GNU cp --reflink=auto may silently copy, so it is never reported as a clone"
t8c="$WORKDIR/t8c"; mkdir -p "$t8c"; d8c="$t8c/nexus.jar"
ln() { return 1; }
cp() { if [[ "$1" == --reflink=auto ]]; then shift; command cp "$@"; else return 1; fi; }   # GNU-like: only --reflink=auto works
gate_jar_cache_install k7 "$entry" "$d8c"
unset -f cp
[[ "$_GATE_JAR_PLACED" == clone-or-copy ]] && ok "a --reflink=auto placement is labelled clone-or-copy" || bad "label for --reflink=auto: $_GATE_JAR_PLACED"
cmp -s "$d8c" "$entry/jar" && ok "and the bytes are right" || bad "bytes differ after the reflink=auto path"
rm -f "$d8c" "$t8c/.gate-jar-key"
cp() { if [[ "$1" == -* ]]; then return 1; else command cp "$@"; fi; }   # no flag works: a plain copy
gate_jar_cache_install k7 "$entry" "$d8c"
unset -f cp ln
[[ "$_GATE_JAR_PLACED" == copy ]] && ok "a plain-copy placement is labelled copy" || bad "label for a plain copy: $_GATE_JAR_PLACED"

echo "Test 9 (nexus-k6zxp): unshare breaks the link before an in-place rewrite, so the entry's bytes survive"
rm -f "$dest" "$t8/.gate-jar-key"; gate_jar_cache_install k7 "$entry" "$dest"
sum9="$(shasum -a 256 "$entry/jar" | cut -c1-64)"
gate_jar_cache_unshare "$dest"
[[ ! -e "$dest" && ! -e "$t8/.gate-jar-key" ]] && ok "a shared target jar (and its marker) is unlinked" || bad "shared target survived unshare"
[[ -f "$entry/jar" && "$(shasum -a 256 "$entry/jar" | cut -c1-64)" == "$sum9" ]] && ok "the entry is untouched by the unshare" || bad "entry changed"
cp "$entry/jar" "$dest"; chmod u+w "$dest"
gate_jar_cache_unshare "$dest"
[[ -f "$dest" ]] && ok "a private writable jar is left alone" || bad "private jar was removed"
# the in-place writer the jar plugin is: it truncates the existing inode
rm -f "$dest"; gate_jar_cache_install k7 "$entry" "$dest"
chmod u+w "$dest"        # the worst case: a writer that does not respect the mode
: > "$dest"
[[ ! -s "$entry/jar" ]] && ok "control: WITHOUT unshare an in-place rewrite does reach the entry through the link" || bad "control failed: the link was not shared"
rm -rf "$r7/k7"
gate_jar_cache_store "$repo" k7 "$j7b" "ref-9"
rm -f "$dest" "$t8/.gate-jar-key"; gate_jar_cache_install k7 "$r7/k7" "$dest"
sum9="$(shasum -a 256 "$r7/k7/jar" | cut -c1-64)"
gate_jar_cache_unshare "$dest"; chmod u+w "$dest" 2>/dev/null; python3 - "$dest" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as zf:
    zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
PY
[[ "$(shasum -a 256 "$r7/k7/jar" | cut -c1-64)" == "$sum9" ]] && ok "WITH unshare the same in-place rewrite leaves the entry's bytes alone" || bad "entry was altered through the link"
unset NX_GATE_JAR_CACHE

echo
echo "gate-jar-cache_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
