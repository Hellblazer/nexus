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
# service/src/main/**, service/src/build/** (the jOOQ codegen runner and its
# generator files), service/mvnw, service/.mvn/** (tracked, modified and
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
# --lab-run <class> (nexus-t6t9f): on a lab pool host (chas, qwent) pass it
# instead of wrapping this script in lab-run, exactly as scripts/mvnw-leased.sh
# does (nexus-iqmih). The lease is then taken BEFORE the pool grant, so a build
# waiting its turn holds no cores, and the Maven build runs as `lab-run -c <class>
# -- ./mvnw ...`, which makes it visible to the pool scheduler (a bare run is
# not). A cache HIT starts no lab-run at all: the jar is already built and no
# grant is taken for it. The build runs as its own process group, recorded as the
# lease's pgid, with INT/TERM/HUP/QUIT forwarded to it; release.properties is
# restored and the lease released only once that group is gone, after the group
# has been re-polled for 3 s following a KILL escalation. If it is still alive at
# exit, BOTH steps are skipped and a WARNING on stderr says so (the stamp stays
# until a later run or a hand restore). A signal ends the script with exit 130
# (TERM used to give 143). See scripts/lib/leased-build.sh.
#
#   usage: scripts/build-gate-jar.sh [--lab-run <class>] [extra mvn args...]

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
props="$repo_root/service/src/main/resources/META-INF/nexus/release.properties"

# shellcheck source=./lib/build-lease.sh disable=SC1091
source "$repo_root/scripts/lib/build-lease.sh"
# shellcheck source=./lib/release-props-lease.sh disable=SC1091
source "$repo_root/scripts/lib/release-props-lease.sh"
# shellcheck source=./lib/gate-jar-cache.sh disable=SC1091
source "$repo_root/scripts/lib/gate-jar-cache.sh"
# shellcheck source=./lib/leased-build.sh disable=SC1091
source "$repo_root/scripts/lib/leased-build.sh"

lab_class=""
if [[ "${1:-}" == "--lab-run" ]]; then
    leased_build_check_lab_run build-gate-jar.sh "${2:-}"
    lab_class="$2"
    shift 2
fi

# Inside a lab-run grant a hand run refuses at once instead of waiting
# (build_lease_caller_wait, nexus-dfg0q).
build_lease_acquire_wait service "$(build_lease_caller_wait)" build-gate-jar.sh "$@" || {
    rc=$?
    [[ $rc -ne 75 ]] || build_lease_grant_refusal_hint build-gate-jar.sh "take the lease before the grant: scripts/build-gate-jar.sh --lab-run <class> ... outside lab-run"
    exit "$rc"
}
# Release on every exit path from here on, and never while the build is alive
# (scripts/lib/leased-build.sh). The finish command below is replaced once there
# is a stamp to restore. Without this a failure in the key computation would
# leak the lease until stale reclaim.
leased_build_init build-gate-jar.sh service

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
# shellcheck disable=SC2034  # read by the EXIT trap in scripts/lib/leased-build.sh
LEASED_BUILD_FINISH=(release_props_restore_and_release "$props" "$backup" service)

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
# stay what they were (nexus-k6zxp).
gate_jar_cache_unshare "$repo_root/service/target/$jar_name"

cd "$repo_root/service"
# -Dnexus.jlink.skip=true: the jlink image is not part of the gate jar and nothing
# reads it here; skipping it saves its writes per miss (service/pom.xml, nexus-0a1hw).
# A non-zero exit exits this script with the same code (set -e); the EXIT path
# restores release.properties and releases the lease.
leased_build_run "$lab_class" ./mvnw -q package -DskipTests -Dnexus.jlink.skip=true "$@"

built_jar="$repo_root/service/target/$jar_name"
test -f "$built_jar" || { echo "build-gate-jar.sh: mvnw package reported success but $built_jar does not exist" >&2; exit 1; }
if [[ -n "$cache_key" ]]; then
    # Best-effort: the store warns and returns 0 on a cache that cannot be written.
    gate_jar_cache_store "$repo_root" "$cache_key" "$built_jar" "$build_ref" \
        || echo "gate jar cache not written; continuing without it" >&2
fi
# The build just (re)published the jOOQ generated trees to the shared cache
# (service/pom.xml, nexus-fkhd3); keep it bounded.
gate_jar_cache_prune_jooq "$repo_root"
echo "built $built_jar"
