#!/usr/bin/env bash
# tests/e2e/migration-rehearsal/run_sh_guard_test.sh — wiring tests for
# run.sh's OWN release.properties guard/stamp/lease code (bead nexus-iexvl,
# critic finding 3 on commit 40963aeb5): scripts/lib/release-props-lease_test.sh
# proves the SHARED LIBRARY's guard/snapshot/stamp/restore ordering is
# correct in isolation, but nothing exercised run.sh's OWN hand-rolled
# wiring around it (NX_STAMP_LEASE_HELD, the trap chain, the point where
# RELEASE_PROPS_SNAPSHOT is populated) until this file.
#
# Runs the REAL, unmodified tests/e2e/migration-rehearsal/run.sh, copied
# byte-for-byte into a throwaway git repo (same technique as
# scripts/build-gate-jar_test.sh) with `docker`/`uv` replaced by recording
# stubs and NEXUS_PREV_RELEASE/NEXUS_PREV_ENGINE_TAG set so the git-tag-
# derivation helpers (which need real release history) are never called.
# No native build, no real docker, no real `uv build`, no `nx` — every
# external command a --candidate-migration invocation would run before the
# point these tests probe is stubbed or bypassed. The hard-coded
# /tmp/nexus-e2e-locks LOCKDIR is the ONE line patched in the copy (sed),
# purely for filesystem isolation from any real concurrent invocation on
# this box; it is not part of what these tests are about.
#
# Test 1: a pre-dirtied release.properties refuses (rc 75, REFUSED),
#   docker/uv are never invoked, and the file is left exactly as dirtied.
# Test 2: the nexus-iexvl reproduction AT RUN.SH'S OWN LEVEL. A concurrent
#   "stamper B" dirties release.properties and holds the lease for a
#   couple of seconds before restoring; a real run.sh invocation ("A") is
#   started while B's dirty bytes are on disk. With the fix, A takes its
#   restore snapshot only AFTER the lease is its own and the guard has
#   passed (i.e. after B has already restored) — final tree == true
#   baseline. Reverting run.sh's fix (restoring commit 40963aeb5's
#   unconditional top-of-script `RELEASE_PROPS_SNAPSHOT="$(mktemp ...)"; cp
#   ...` in place of the current empty declaration, and the direct
#   build_lease_acquire_wait/release_props_guard_clean/stamp sequence in
#   place of the release_props_stamp_under_lease call) makes A's snapshot
#   capture B's IN-FLIGHT DIRTY bytes instead — this test then fails with
#   the final tree equal to B's abandoned stamp, not the true baseline.
#
# Run with: bash tests/e2e/migration-rehearsal/run_sh_guard_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/run_sh_guard_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
PASS=0; FAIL=0
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

repo="$WORKDIR/repo"
props_rel="service/src/main/resources/META-INF/nexus/release.properties"
mkdir -p \
  "$repo/tests/e2e/migration-rehearsal" \
  "$repo/tests/e2e/lib" \
  "$repo/scripts/lib" \
  "$repo/src/nexus" \
  "$repo/service/src/main/resources/META-INF/nexus"

# Byte-for-byte copies of the REAL files under test / their real
# dependencies — never re-typed, so this test cannot silently drift from
# what actually ships.
cp "$REPO_ROOT/tests/e2e/migration-rehearsal/run.sh" "$repo/tests/e2e/migration-rehearsal/run.sh"
cp "$REPO_ROOT/tests/e2e/lib/exit_diagnostics.sh" "$repo/tests/e2e/lib/exit_diagnostics.sh"
cp "$REPO_ROOT/tests/e2e/lib/lock.sh" "$repo/tests/e2e/lib/lock.sh"
cp "$REPO_ROOT/scripts/lib/build-lease.sh" "$repo/scripts/lib/build-lease.sh"
cp "$REPO_ROOT/scripts/lib/release-props-lease.sh" "$repo/scripts/lib/release-props-lease.sh"

# The ONE test-only edit to the copied run.sh: isolate its hard-coded
# /tmp/nexus-e2e-locks LOCKDIR to this fixture, so this test never
# contends with a real concurrent migration-rehearsal invocation on the
# same box. Unrelated to the guard/stamp/lease logic under test.
sed -i.bak "s#/tmp/nexus-e2e-locks#${WORKDIR}/e2e-locks#g" "$repo/tests/e2e/migration-rehearsal/run.sh"
rm -f "$repo/tests/e2e/migration-rehearsal/run.sh.bak"

echo 'REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (9, 9, 9)' > "$repo/src/nexus/engine_version.py"
printf 'release_version=\nbuild_ref=\nname=nexus\n' > "$repo/$props_rel"

git -C "$repo" init -q
git -C "$repo" -c user.email=test@test -c user.name=test add -A
git -C "$repo" -c user.email=test@test -c user.name=test commit -q -m init

DOCKER_CALLS="$WORKDIR/docker-calls"
UV_CALLS="$WORKDIR/uv-calls"
mkdir -p "$WORKDIR/bin"

# `uv` stub: only `uv build --wheel` succeeds (and deliberately writes NO
# wheel into dist/ — run.sh's own post-native-build `ls dist/conexus-*.whl`
# check then fails loud and exits 1, which is what stops Test 2 cheaply,
# right after the point it means to probe, with no further stubbing
# needed). Anything else refuses loudly rather than silently no-op'ing.
cat > "$WORKDIR/bin/uv" <<STUB
#!/usr/bin/env bash
echo "uv \$*" >> "$UV_CALLS"
if [ "\$1" = "build" ]; then exit 0; fi
echo "STUB-UV: unexpected invocation: uv \$*" >&2
exit 18
STUB
chmod +x "$WORKDIR/bin/uv"

# `docker` stub: `info --format ...` (builder-heap sizing) and exactly one
# `run` (the native-image build) succeed; nothing else is expected to run
# before the wheel-existence check above aborts the leg.
cat > "$WORKDIR/bin/docker" <<STUB
#!/usr/bin/env bash
echo "docker \$*" >> "$DOCKER_CALLS"
if [ "\$1" = "info" ]; then echo 8589934592; exit 0; fi
if [ "\$1" = "run" ]; then exit 0; fi
echo "STUB-DOCKER: unexpected invocation: docker \$*" >&2
exit 17
STUB
chmod +x "$WORKDIR/bin/docker"

run_a() {
  # NEXUS_PREV_RELEASE/NEXUS_PREV_ENGINE_TAG: short-circuits run.sh's
  # git-tag-derivation helpers (${VAR:-$(...)} never calls the command
  # substitution when VAR is already non-empty) — this fixture has no
  # release-tag history for them to walk.
  env -i \
    NX_NO_TELEMETRY=1 \
    PATH="$WORKDIR/bin:/usr/bin:/bin:/usr/local/bin" \
    HOME="$HOME" \
    TMPDIR="${TMPDIR:-/tmp}" \
    NEXUS_PREV_RELEASE=1.0.0 \
    NEXUS_PREV_ENGINE_TAG=engine-service-v0.0.1 \
    NX_BUILD_LEASE_WAIT="${1:-10}" \
    bash "$repo/tests/e2e/migration-rehearsal/run.sh" --candidate-migration
}

echo "Test 1: pre-dirtied release.properties refuses before any build"
printf 'release_version=9.9.8\nbuild_ref=abandoned-B\nname=nexus\n' > "$repo/$props_rel"
out1="$(run_a 5 2>&1)"; rc1=$?
[[ $rc1 -eq 75 ]] && ok "refused with rc 75" || bad "expected rc 75, got $rc1: $out1"
[[ "$out1" == *"REFUSED"* ]] && ok "refusal is disclosed (REFUSED)" || bad "no REFUSED text: $out1"
[[ ! -s "$DOCKER_CALLS" ]] && ok "docker never invoked" || bad "docker was invoked: $(cat "$DOCKER_CALLS" 2>/dev/null)"
[[ ! -s "$UV_CALLS" ]] && ok "uv never invoked" || bad "uv was invoked: $(cat "$UV_CALLS" 2>/dev/null)"
actual1="$(cat "$repo/$props_rel")"
[[ "$actual1" == "release_version=9.9.8"* ]] && ok "tree left exactly as dirtied" || bad "tree changed after refusal: $actual1"

echo "Test 2: nexus-iexvl reproduction via run.sh's own wiring (concurrent stamper B)"
git -C "$repo" checkout -q -- "$props_rel"
baseline="$(cat "$repo/$props_rel")"
ready="$WORKDIR/b-dirty-ready"
rm -f "$ready" "$DOCKER_CALLS" "$UV_CALLS"
(
  # shellcheck source=/dev/null
  source "$repo/scripts/lib/release-props-lease.sh"
  build_lease_acquire_wait service 10 fake-B-holder >/dev/null 2>&1
  printf 'release_version=B-DIRTY\nbuild_ref=B-DIRTY\nname=nexus\n' > "$repo/$props_rel"
  touch "$ready"
  sleep 2
  printf '%s' "$baseline" > "$repo/$props_rel"
  build_lease_release service
) &
b_pid=$!
b_ready=0
for _ in $(seq 1 50); do
  if [[ -f "$ready" ]]; then b_ready=1; break; fi
  sleep 0.1
done
if [[ "$b_ready" -eq 1 ]]; then
  ok "stamper B reached its dirty-and-holding state"
else
  bad "stamper B never signaled ready — test setup itself is broken"
fi
out2="$(run_a 10 2>&1)"; rc2=$?
wait "$b_pid"
# A is expected to run.sh's OWN post-native-build wheel-existence check
# (the uv stub deliberately produced no wheel) — exit 1, "no wheel in
# dist/". A different rc here means A took a different path than this
# test assumes and the result below cannot be trusted; say so loudly
# rather than silently asserting on it anyway.
if [[ $rc2 -eq 1 && "$out2" == *"no wheel in dist/"* ]]; then
  ok "A reached the expected post-native-build stopping point (rc 1, no wheel in dist/)"
else
  bad "A did not reach the expected stopping point (rc $rc2): $out2"
fi
final="$(cat "$repo/$props_rel")"
if [[ "$final" == "$baseline" ]]; then
  ok "final tree equals the true baseline — B's abandoned stamp was never adopted"
else
  bad "final tree does not equal the true baseline (nexus-iexvl reproduced): $final"
fi
[[ "$(cat "$DOCKER_CALLS" 2>/dev/null | grep -c '^docker run')" == 1 ]] \
  && ok "exactly one native-build docker run (native build only, nothing further)" \
  || bad "unexpected docker run count: $(cat "$DOCKER_CALLS" 2>/dev/null)"
lease_dir_common="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)/nexus-build-lease/service"
[[ ! -d "$lease_dir_common" ]] && ok "service lease not left held after A exits" || bad "service lease still held: $lease_dir_common"

echo
echo "run_sh_guard_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
