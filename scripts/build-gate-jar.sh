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
# the holder only once that bound is exhausted. See scripts/lib/build-lease.sh.
#
# STAMP GUARDED + HELD FOR ITS WHOLE LIFETIME (nexus-iexvl): once the lease
# above is held, release.properties is checked against git HEAD before this
# script backs anything up. A dirty file at that point means a PRIOR
# process left the tree stamped without going through this same lease
# discipline (the migration-rehearsal --guided/--shakeout-e2e/--candidate-
# migration legs, before their own nexus-iexvl fix) — refuse loudly rather
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
# service/ working-tree content (tracked, modified and untracked non-ignored
# files, via a throwaway git index) plus the stamped release_version; a hit
# copies the cached jar into service/target and prints the build_ref that
# jar carries, a miss builds and stores. "Never rebuild deterministic
# artifacts" (AGENTS.md § CI Cost Discipline), applied to the dev box. A hit
# therefore REUSES a build_ref: the per-run-unique guarantee below holds per
# distinct service/ content, not per invocation — a caller that captured the
# printed value still matches the jar it is given. NX_GATE_JAR_CACHE=off
# disables the cache; NX_GATE_JAR_CACHE=<dir> relocates it. See
# scripts/lib/gate-jar-cache.sh.
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
build_lease_acquire_wait service "${NX_BUILD_LEASE_WAIT:-3600}" build-gate-jar.sh "$@"
# Release on every exit path from here on; the props-restore trap below
# replaces this once there is a stamp to restore. Without it a failure in
# the key computation would leak the lease until stale reclaim.
trap 'build_lease_release service' EXIT

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
# identifies exactly one artifact; it is per distinct service/ content, not
# per invocation.
sha="$(cd "$repo_root" && git rev-parse --short HEAD 2>/dev/null || echo nogit)"
nonce="$(date +%s)-$$"
build_ref="${sha}+${nonce}"

# Cache lookup, AFTER the lease is held (so no concurrent stamp is in the
# tree while the key is computed) and BEFORE this run stamps anything.
# The key covers service/ content; extra mvn args are part of it too, so a
# `-Pfoo` build never serves a plain one.
jar_name="nexus-service-1.0-SNAPSHOT.jar"
cache_key="$(gate_jar_cache_key "$repo_root" "$ver" "$*")"
if cached="$(gate_jar_cache_lookup "$repo_root" "$cache_key")"; then
    mkdir -p "$repo_root/service/target"
    cp "$cached/jar" "$repo_root/service/target/$jar_name"
    build_ref="$(cat "$cached/build_ref")"
    echo "stamped release_version=$ver"
    echo "stamped build_ref=$build_ref"
    echo "gate jar cache HIT key=$cache_key (service/ content unchanged since that build; $cached)"
    echo "built $repo_root/service/target/$jar_name"
    build_lease_release service
    exit 0
fi
echo "gate jar cache MISS key=$cache_key — building"

backup="$(mktemp)"
cp "$props" "$backup"
# Restore on ANY exit path, BEFORE releasing the lease (nexus-iexvl — see
# the header comment): a stamped release.properties left in the tree is a
# tracked-file modification that would follow the developer into their next
# commit, and it would make a subsequent `mvn package` silently produce a
# release-looking JAR. release_props_restore_and_release always restores
# first and releases second, so no other process can ever observe a freed
# lease while this file is still stamped.
trap 'release_props_restore_and_release "$props" "$backup" service' EXIT

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

cd "$repo_root/service"
./mvnw -q package -DskipTests "$@"

built_jar="$repo_root/service/target/$jar_name"
test -f "$built_jar" || { echo "build-gate-jar.sh: mvnw package reported success but $built_jar does not exist" >&2; exit 1; }
gate_jar_cache_store "$repo_root" "$cache_key" "$built_jar" "$build_ref"
echo "built $built_jar"
