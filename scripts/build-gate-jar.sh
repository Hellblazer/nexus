#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build the shaded service JAR that the -m integration gates boot, with
# release_version STAMPED so the client's cloud version probe is satisfied
# legitimately.
#
# WHY THIS EXISTS (nexus-ao29z). release.properties ships release_version BLANK
# by design — it is stamped only at native-release time from the
# engine-service-vX.Y.Z tag — so a plain `mvn package` produces a JAR that
# reports release_version=null on /version. Since the fail-loud cloud probe
# landed (3cb14f96, 2026-07-09) the HttpVectorClient connection path fail-closes
# on exactly that, so every gate that boots a locally built JAR and drives the
# vector client errored at SETUP:
#
#   tests/db/test_http_combined_query_integration.py
#   tests/db/test_write_seam_gate_integration.py
#   tests/db/test_indexer_seam_b_integration.py
#   tests/db/test_frecency_enehl_integration.py
#
# CI's seam job hit this the same day and solved it by stamping (ci.yml, "Stamp
# release_version into the gate JAR"), deliberately NOT by bypassing the probe:
# the gate then exercises a CONFORMANT engine and the hardening stays intact.
# This script is that same step for a developer's machine, so a local run proves
# what CI proves.
#
# The stamp is taken from REQUIRED_ENGINE_VERSION in src/nexus/engine_version.py
# — the ONE floor constant — so it cannot drift when the pin bumps.
#
# ALSO stamps build_ref (nexus-308ph): a per-run artifact-identity
# discriminator, <git short sha>+<per-run nonce>. release_version alone
# cannot distinguish this jar from a pinned release binary built against the
# same floor — both bake the identical value. build_ref is unique to THIS
# invocation, so only the artifact built by THIS run can ever match a value
# THIS run expects (see tests/e2e/local-service-gate.sh's smoke leg, which
# asserts the served /version's build_ref against the value it stamped).
# Printed to stdout as "stamped build_ref=<value>" so a caller can capture it.
#
# The working tree is left CLEAN: release.properties is restored on exit,
# including on failure or interrupt.
#
# SINGLE-BUILDER LEASE (nexus-c00dw): acquires the service build lease
# before touching anything, so this script and a concurrent ./mvnw
# invocation (an orchestrator + a developer agent, historically — see the
# incident this bead records) can never write service/target at the same
# time. The lease is shared by every worktree of the repo and a live holder
# is waited for, bounded by NX_BUILD_LEASE_WAIT (nexus-g6xpa); rc 75 names
# the holder only once that bound is exhausted. Inside a lab-run grant (LAB_CPUS
# set, not GitHub Actions) a held lease refuses at once with 75 instead
# (nexus-dfg0q). See scripts/lib/build-lease.sh.
#
# STAMP GUARDED + HELD FOR ITS WHOLE LIFETIME (nexus-iexvl): once the lease
# above is held, release.properties is checked against git HEAD before this
# script backs anything up. A dirty file at that point means a PRIOR
# process left the tree stamped without going through this same lease
# discipline (the migration-rehearsal stamping legs, before their own nexus-iexvl fix) — refuse loudly rather
# than `cp`-backing-up whatever happens to be on disk, stamping over it,
# and restoring THAT (someone else's) stamp on exit. The restore-then-
# release trap below (release_props_restore_and_release) closes the other
# half of the same incident: this script used to release the lease BEFORE
# restoring the file, leaving its own short window where a second acquirer
# could snapshot a still-stamped tree. See scripts/lib/release-props-lease.sh.
#
# GATE-JAR CACHE (nexus-g6xpa, 2026-09-07): a fresh worktree used to spend
# ~9 minutes here rebuilding a jar byte-identical to the primary's. The
# stamped jar is now cached in the git common dir keyed on the EXACT
# working-tree content of what the jar is built from -- service/pom.xml,
# service/src/main/**, service/mvnw, service/.mvn/** (tracked, modified and
# untracked non-ignored files, via a throwaway index; src/test, docs and
# scripts under service/ are NOT part of it, nexus-fkhd3) -- plus the
# stamped release_version; a hit
# links the cached jar into service/target (copying only where linking fails,
# and not at all when service/target already holds it; nexus-k6zxp) and prints
# the build_ref that jar carries, a miss builds and stores. "Never rebuild deterministic
# artifacts" (.claude/rules/ci-and-runners.md § CI Cost Discipline), applied to the dev box. A hit
# therefore REUSES a build_ref: the per-run-unique guarantee below holds per
# distinct build-input content, not per invocation — a caller that captured the
# printed value still matches the jar it is given. NX_GATE_JAR_CACHE=off
# disables the cache; NX_GATE_JAR_CACHE=<dir> relocates it. See
# scripts/lib/gate-jar-cache.sh.
#
# BUILD DIRECTORY (nexus-0a1hw). A miss used to build entirely in service/target:
# on chas that was 316 MiB written to the NVMe per miss; on a laptop ~245 MiB (the
# shaded jar ~131 MB, the jlink image ~83 MB, ~15-35 MiB of other Maven output).
# Two changes, with different sizes. (1) -Dnexus.jlink.skip=true, on every host,
# skips the jlink image nothing reads: ~83 MiB, the larger saving. (2) On a lab host
# (Linux, inside a lab-run grant, a usable /dev/shm, a readable grant memory limit,
# a jOOQ cache that can serve the build) the build goes to a per-build tmpfs
# directory (-Dnexus.build.dir, service/pom.xml) removed on exit: ~15-35 MiB of
# Maven output. What stays: the stamped jar (~131 MB) is still written once per
# key, as the cache entry, and then linked into service/target as on a hit; that is
# the floor. The jOOQ codegen on a jOOQ cache MISS (~350 MB in the container
# overlay) is untouched by either change, so the auto decision stays on disk when
# the cache cannot serve the build and a tmpfs build that did run the codegen says
# so. The post-change chas figure comes from the chas proof run, not from this
# comment. Anywhere else (macOS, a laptop, outside a grant) the build stays in
# service/target. NX_BUILD_TMPFS=off|on|<dir> overrides; an explicit on or <dir>
# prints a warning where auto would have refused. A tmpfs build killed for memory
# (exit 137) or out of space is retried once on disk. The build runs in its own
# process group, which the exit trap stops before it removes the directory, so a
# SIGTERM to this script leaves neither a Maven nor a directory behind. See
# scripts/lib/build-tmpfs.sh. service/target then holds the jar and its marker but
# no classes: a later `mvnw-leased.sh test` recompiles (and restores the jOOQ trees
# from the shared cache).
#
#   usage: scripts/build-gate-jar.sh [extra mvn args...]

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
props="$repo_root/service/src/main/resources/META-INF/nexus/release.properties"

# shellcheck source=./lib/build-lease.sh disable=SC1091
source "$repo_root/scripts/lib/build-lease.sh"
# shellcheck source=./lib/release-props-lease.sh disable=SC1091
source "$repo_root/scripts/lib/release-props-lease.sh"
# shellcheck source=./lib/gate-jar-cache.sh disable=SC1091
source "$repo_root/scripts/lib/gate-jar-cache.sh"
# shellcheck source=./lib/build-tmpfs.sh disable=SC1091
source "$repo_root/scripts/lib/build-tmpfs.sh"
# This script owns where the build writes (it must find the jar it built): a
# caller's own -Dnexus.build.dir would send the jar somewhere it does not look.
if [[ " $* " == *" -Dnexus.build.dir"* ]]; then
    echo "build-gate-jar.sh: do not pass -Dnexus.build.dir; set NX_BUILD_TMPFS=off|on|<dir> instead (nexus-0a1hw)" >&2
    exit 64
fi
# Inside a lab-run grant a hand run refuses at once instead of waiting
# (build_lease_caller_wait, nexus-dfg0q).
build_lease_acquire_wait service "$(build_lease_caller_wait)" build-gate-jar.sh "$@" || {
    rc=$?
    [[ $rc -ne 75 ]] || build_lease_grant_refusal_hint build-gate-jar.sh
    exit "$rc"
}
# Release on every exit path from here on; the props-restore trap below
# replaces this once there is a stamp to restore. Without it a failure in
# the key computation would leak the lease until stale reclaim.
trap 'build_lease_release service' EXIT
# A signal ends the script through its EXIT trap (which stops the build's process
# group, removes the tmpfs directory, restores the stamp and releases the lease)
# instead of leaving a running Maven behind.
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

test -f "$props" || { echo "release.properties missing at $props" >&2; exit 1; }

# nexus-iexvl: now that the lease is ours, nobody else can legitimately be
# mid-stamp — a dirty file here means a prior process left the tree
# stamped without holding this lease for its whole stamp lifetime. Refuse
# before ever backing anything up.
if ! release_props_guard_clean "$props" service; then
    build_lease_release service
    exit 75
fi

ver=$(cd "$repo_root" && python3 -c "
import pathlib, re
s = pathlib.Path('src/nexus/engine_version.py').read_text()
m = re.search(r'REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)', s)
assert m, 'REQUIRED_ENGINE_VERSION not parseable — fix the regex before stamping'
print('.'.join(m.groups()))
")

# nexus-308ph: git short sha + a per-build nonce (epoch seconds + PID) —
# unique to the BUILD that stamped it, so no other build (dev or release)
# can bake the identical value. A cache hit (nexus-g6xpa) hands back the
# jar that build produced together with its build_ref, so the value still
# identifies exactly one artifact; it is per distinct build-input content, not
# per invocation.
sha="$(cd "$repo_root" && git rev-parse --short HEAD 2>/dev/null || echo nogit)"
nonce="$(date +%s)-$$"
build_ref="${sha}+${nonce}"

# Cache lookup, AFTER the lease is held (so no concurrent stamp is in the
# tree while the key is computed) and BEFORE this run stamps anything.
# The key covers the build inputs; extra mvn args are part of it too, so a
# `-Pfoo` build never serves a plain one.
jar_name="nexus-service-1.0-SNAPSHOT.jar"
# The cache is best-effort end to end: a key that cannot be computed, an entry
# that cannot be read or copied whole, a root that cannot be written, all fall
# through to a plain build (nexus-fkhd3 review).
cache_key=""
if ! cache_key="$(gate_jar_cache_key "$repo_root" "$ver" "$*")"; then
    echo "gate jar cache disabled for this run: the key could not be computed" >&2
    cache_key=""
fi
target_jar="$repo_root/service/target/$jar_name"
if [[ -n "$cache_key" ]] && cached="$(gate_jar_cache_lookup "$repo_root" "$cache_key")"; then
    mkdir -p "$repo_root/service/target"
    # Install, then re-verify the RESULT: an entry pruned or damaged between the
    # lookup's check and this read must become a rebuild, not a bad jar.
    # gate_jar_cache_install writes nothing when service/target already holds
    # this key's jar, links the entry's jar in otherwise, and copies only where
    # linking fails (nexus-k6zxp).
    if hit_ref="$(cat "$cached/build_ref" 2>/dev/null)" && [[ -n "$hit_ref" ]] \
        && gate_jar_cache_install "$cache_key" "$cached" "$target_jar" \
        && _gate_jar_cache_jar_ok "$target_jar" "$hit_ref"; then
        echo "stamped release_version=$ver"
        echo "stamped build_ref=$hit_ref"
        echo "gate jar cache HIT key=$cache_key (build inputs unchanged since that build; $cached; jar $_GATE_JAR_PLACED)"
        echo "built $target_jar"
        build_lease_release service
        exit 0
    fi
    rm -f "$target_jar" "$repo_root/service/target/.gate-jar-key"
    echo "gate jar cache entry $cached could not be used whole; building" >&2
fi
echo "gate jar cache MISS key=${cache_key:-none} — building"

backup="$(mktemp)"
cp "$props" "$backup"
# Restore on ANY exit path, BEFORE releasing the lease (nexus-iexvl — see
# the header comment): a stamped release.properties left in the tree is a
# tracked-file modification that would follow the developer into their next
# commit, and it would make a subsequent `mvn package` silently produce a
# release-looking JAR. release_props_restore_and_release always restores
# first and releases second, so no other process can ever observe a freed
# lease while this file is still stamped.
trap 'build_tmpfs_cleanup; rm -f "${mvn_log:-}"; release_props_restore_and_release "$props" "$backup" service' EXIT

tmp="$(mktemp)"
grep -Ev '^release_version=|^build_ref=' "$props" > "$tmp"
printf 'release_version=%s\n' "$ver" >> "$tmp"
printf 'build_ref=%s\n' "$build_ref" >> "$tmp"
mv "$tmp" "$props"
echo "stamped release_version=$ver"
echo "stamped build_ref=$build_ref"
# tests/e2e/local-service-gate.sh stamps its own nonce INLINE rather than
# calling this script: its restore choreography differs (gate-owned cleanup
# vs this script's byte-snapshot trap), and the gate must hold the expected
# value in its own process for the smoke-leg compare. Intentional
# duplication of the <sha>+<epoch>-<pid> shape — keep the two in step.

# The jar plugin rewrites an existing service/target jar IN PLACE, and a cache
# hit or store leaves that path hard-linked to the cache entry: unlink a shared
# (or read-only) jar first so the build writes a new inode and the entry's bytes
# stay what they were (nexus-k6zxp). Done on the tmpfs path too, though the build
# no longer touches this path: a failed build must not leave an older jar that
# looks current.
gate_jar_cache_unshare "$target_jar"

# Where Maven writes (nexus-0a1hw): a per-build tmpfs directory on a lab host,
# else service/target. Removed by the EXIT trap above, which first stops the
# build's process group.
build_tmpfs_prepare "$repo_root" "$@"
[[ -z "$BUILD_TMPFS_WARN" ]] || echo "build-gate-jar.sh: warning: building in tmpfs although: $BUILD_TMPFS_WARN" >&2
mvn_extra=(-Dnexus.jlink.skip=true)
jooq_before=""
mvn_log=""
if [[ -n "$BUILD_TMPFS_DIR" ]]; then
    mvn_extra+=("-Dnexus.build.dir=$BUILD_TMPFS_DIR")
    build_dir="$BUILD_TMPFS_DIR"
    echo "gate jar build directory: $build_dir (tmpfs; $BUILD_TMPFS_WHY)"
    jooq_before="$(build_tmpfs_jooq_entries "$BUILD_TMPFS_JOOQ_ROOT")"
    mvn_log="$(mktemp)"
else
    build_dir="$repo_root/service/target"
    echo "gate jar build directory: service/target ($BUILD_TMPFS_WHY)"
fi

cd "$repo_root/service"
rc=0
build_tmpfs_run "$mvn_log" ./mvnw -q package -DskipTests "${mvn_extra[@]}" "$@" || rc=$?
if [[ $rc -ne 0 && -n "$BUILD_TMPFS_DIR" ]]; then
    # A tmpfs build that the grant's memory killed (137) or that ran out of space is
    # the one failure a disk build avoids: retry once there, say so, and never loop.
    reason=""
    if [[ $rc -eq 137 ]]; then reason="exit 137, killed for memory"
    elif grep -qi 'no space left' "$mvn_log" 2>/dev/null; then reason="out of space (rc $rc)"; fi
    if [[ -n "$reason" ]]; then
        echo "build-gate-jar.sh: the tmpfs build failed ($reason) in $BUILD_TMPFS_DIR; retrying once on disk in service/target" >&2
        build_tmpfs_cleanup
        mvn_extra=(-Dnexus.jlink.skip=true)
        build_dir="$repo_root/service/target"
        BUILD_TMPFS_WHY="retry after a failed tmpfs build"
        echo "gate jar build directory: service/target ($BUILD_TMPFS_WHY)"
        rc=0
        build_tmpfs_run "" ./mvnw -q package -DskipTests "${mvn_extra[@]}" "$@" || rc=$?
    fi
fi
if [[ $rc -ne 0 ]]; then
    [[ -z "$BUILD_TMPFS_DIR" ]] || echo "build-gate-jar.sh: the build failed (rc $rc) in $BUILD_TMPFS_DIR; rc 137 or a 'No space left' means the grant's memory held too little for a tmpfs build: rerun with NX_BUILD_TMPFS=off to build on disk" >&2
    exit "$rc"
fi
if [[ -n "$BUILD_TMPFS_DIR" ]]; then
    # The auto gate cannot see the pom's input hash: say when this build's codegen ran.
    jooq_after="$(build_tmpfs_jooq_entries "$BUILD_TMPFS_JOOQ_ROOT")"
    if [[ "$jooq_after" != "$jooq_before" ]]; then
        echo "build-gate-jar.sh: the jOOQ codegen ran in this tmpfs build (new jOOQ cache entry: $(comm -13 <(printf '%s\n' "$jooq_before") <(printf '%s\n' "$jooq_after") | tr '\n' ' ')); its container writes ~350 MB regardless of the build directory"
    fi
fi

built_jar="$build_dir/$jar_name"
test -f "$built_jar" || { echo "build-gate-jar.sh: mvnw package reported success but $built_jar does not exist" >&2; exit 1; }
if [[ -z "$BUILD_TMPFS_DIR" ]]; then
    if [[ -n "$cache_key" ]]; then
        # Best-effort: the store warns and returns 0 on a cache that cannot be written.
        gate_jar_cache_store "$repo_root" "$cache_key" "$built_jar" "$build_ref" \
            || echo "gate jar cache not written; continuing without it" >&2
    fi
else
    # The jar was built off the disk. Store it (one copy, across filesystems, per
    # key) and link the entry into service/target as a hit does; the entry must be
    # the jar THIS run built, whose build_ref was printed above, else (a peer
    # stored the same key first, or no cache) put a private copy there.
    mkdir -p "$repo_root/service/target"
    placed=""
    if [[ -n "$cache_key" ]]; then
        gate_jar_cache_store "$repo_root" "$cache_key" "$built_jar" "$build_ref" \
            || echo "gate jar cache not written; continuing without it" >&2
        if cached="$(gate_jar_cache_lookup "$repo_root" "$cache_key")" \
            && gate_jar_cache_install "$cache_key" "$cached" "$target_jar" \
            && _gate_jar_cache_jar_ok "$target_jar" "$build_ref"; then
            placed="$_GATE_JAR_PLACED"
        fi
    fi
    if [[ -z "$placed" ]]; then
        rm -f "$target_jar" "$repo_root/service/target/.gate-jar-key"
        tmp_jar="$repo_root/service/target/.gate-jar.tmp.$$"
        _gate_jar_cache_place "$built_jar" "$tmp_jar" && mv -f "$tmp_jar" "$target_jar" \
            || { rm -f "$tmp_jar"; echo "build-gate-jar.sh: could not place the built jar at $target_jar" >&2; exit 1; }
        placed="$_GATE_JAR_PLACED (private)"
    fi
    echo "gate jar placed in service/target ($placed); the build directory goes away on exit"
fi
# The build just (re)published the jOOQ generated trees to the shared cache
# (service/pom.xml, nexus-fkhd3); keep it bounded.
gate_jar_cache_prune_jooq "$repo_root"
echo "built $target_jar"
