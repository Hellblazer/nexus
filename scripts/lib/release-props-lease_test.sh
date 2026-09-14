#!/usr/bin/env bash
# scripts/lib/release-props-lease_test.sh — unit-level shell tests for
# release-props-lease.sh (bead nexus-iexvl). Self-provisioning: builds its
# own throwaway fake-repo tmpdir with a REAL (but tiny, local) git checkout
# so release_props_guard_clean has a HEAD blob to compare against; never
# touches the real checkout's service/ tree. Run directly with bash:
#   bash scripts/lib/release-props-lease_test.sh
#
# Mirrors scripts/lib/build-lease_test.sh's ok/bad/PASS/FAIL convention.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/release_props_lease_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

PASS=0
FAIL=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

# _fake_repo <dir> — a real git repo (so HEAD:<path> resolves) with a copy
# of both library files under scripts/lib/, and a tracked fake
# release.properties with blank release_version/build_ref (the real
# file's shipped shape) committed at HEAD.
_fake_repo() {
    local repo="$1"
    mkdir -p "$repo/scripts/lib" "$repo/service/src/main/resources/META-INF/nexus"
    cp "$HERE/build-lease.sh" "$repo/scripts/lib/build-lease.sh"
    cp "$HERE/release-props-lease.sh" "$repo/scripts/lib/release-props-lease.sh"
    printf 'release_version=\nbuild_ref=\n' > "$repo/service/src/main/resources/META-INF/nexus/release.properties"
    git -C "$repo" init -q
    git -C "$repo" config user.email test@example.invalid
    git -C "$repo" config user.name "release-props-lease_test"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m init
}

PROPS_RELPATH="service/src/main/resources/META-INF/nexus/release.properties"

# _lease_dir_for <repo> <name> — the ACTUAL on-disk lease directory for a
# fake repo, mirroring build-lease.sh's own _build_lease_root: since
# _fake_repo below creates a REAL git repo (needed for guard_clean's
# `git show HEAD:...`), the lease lives under the git COMMON dir
# (<repo>/.git/nexus-build-lease/<name>), never under
# <repo>/service/.build-lease/<name> (that fallback only applies outside a
# git repo — see scripts/lib/build-lease_test.sh's non-git fake repos,
# which is why ITS hardcoded path works and a git-backed one here must not
# assume the same layout).
_lease_dir_for() {
    local repo="$1" name="$2" common
    common="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
    printf '%s/nexus-build-lease/%s\n' "$common" "$name"
}

# ── Test 1: guard passes on a clean tracked file ──────────────────────────
echo "Test 1: guard_clean passes when the file matches HEAD"
repo1="$WORKDIR/repo1"
_fake_repo "$repo1"
props1="$repo1/$PROPS_RELPATH"
out1="$(bash -c "source '$repo1/scripts/lib/release-props-lease.sh'; release_props_guard_clean '$props1' service" 2>&1)"
rc1=$?
if [[ $rc1 -eq 0 ]]; then ok "clean file passes the guard (rc 0)"; else bad "clean file refused (rc $rc1): $out1"; fi
if [[ -z "$out1" ]]; then ok "no output on a clean pass"; else bad "unexpected output on a clean pass: $out1"; fi

# ── Test 2: guard refuses a pre-stamped (dirty) tree ──────────────────────
echo "Test 2: guard_clean refuses when the file differs from HEAD"
repo2="$WORKDIR/repo2"
_fake_repo "$repo2"
props2="$repo2/$PROPS_RELPATH"
printf 'release_version=0.1.117\nbuild_ref=deadbeef+123-456\n' > "$props2"
out2="$(bash -c "source '$repo2/scripts/lib/release-props-lease.sh'; release_props_guard_clean '$props2' service" 2>&1)"
rc2=$?
if [[ $rc2 -eq 75 ]]; then ok "dirty file refused (rc 75)"; else bad "dirty file returned rc $rc2 (expected 75): $out2"; fi
if [[ "$out2" == *"REFUSED"* ]]; then ok "refusal is disclosed (REFUSED)"; else bad "no REFUSED text: $out2"; fi
if [[ "$out2" == *"$props2"* ]]; then ok "refusal names the file"; else bad "refusal does not name the file: $out2"; fi
if [[ "$out2" == *"release_version=0.1.117"* ]]; then ok "refusal shows the stamped (current) value"; else bad "refusal does not show current content: $out2"; fi
if [[ "$out2" == *"git checkout"* ]]; then ok "refusal names the git checkout remedy"; else bad "refusal missing git checkout remedy: $out2"; fi
if [[ "$out2" == *"service"* && "$out2" == *"build lease"* ]]; then
    ok "refusal names the lease to check before restoring"
else
    bad "refusal does not name the lease to check: $out2"
fi
# The file itself must be untouched by a refused guard call.
if [[ "$(cat "$props2")" == "release_version=0.1.117"* ]]; then
    ok "guard is read-only: dirty file left exactly as found"
else
    bad "guard mutated the file it refused: $(cat "$props2")"
fi

# ── Test 2b: guard works with a CWD-RELATIVE path (as run.sh calls it) ───
# tests/e2e/migration-rehearsal/run.sh cds to the repo root and passes
# RELEASE_PROPS as a plain relative path, not an absolute one -- the guard
# must resolve that the same way an absolute path would.
echo "Test 2b: guard_clean works from a cwd-relative props path"
repo2b="$WORKDIR/repo2b"
_fake_repo "$repo2b"
out2b_clean="$(cd "$repo2b" && bash -c "source 'scripts/lib/release-props-lease.sh'; release_props_guard_clean '$PROPS_RELPATH' service" 2>&1)"
rc2b_clean=$?
if [[ $rc2b_clean -eq 0 ]]; then ok "relative-path guard passes on a clean file"; else bad "relative-path guard refused a clean file (rc $rc2b_clean): $out2b_clean"; fi
printf 'release_version=relative-dirty\nbuild_ref=x\n' > "$repo2b/$PROPS_RELPATH"
out2b_dirty="$(cd "$repo2b" && bash -c "source 'scripts/lib/release-props-lease.sh'; release_props_guard_clean '$PROPS_RELPATH' service" 2>&1)"
rc2b_dirty=$?
if [[ $rc2b_dirty -eq 75 ]]; then ok "relative-path guard refuses a dirty file (rc 75)"; else bad "relative-path guard returned rc $rc2b_dirty (expected 75): $out2b_dirty"; fi
if [[ "$out2b_dirty" == *"relative-dirty"* ]]; then ok "relative-path refusal shows the stamped value"; else bad "relative-path refusal missing content: $out2b_dirty"; fi

# ── Test 3: stamp_under_lease / restore_and_release round trip ───────────
echo "Test 3: stamp_under_lease stamps under the lease; restore_and_release restores + releases in order"
repo3="$WORKDIR/repo3"
_fake_repo "$repo3"
props3="$repo3/$PROPS_RELPATH"
baseline3="$(cat "$props3")"
leasedir3="$(_lease_dir_for "$repo3" service)"

out3="$(bash -c "
    source '$repo3/scripts/lib/release-props-lease.sh'
    snap=\$(release_props_stamp_under_lease '$props3' service 5 release_version=9.9.9 build_ref=test-ref) || exit \$?
    echo \"SNAP=\$snap\"
    echo \"MID=\$(cat '$props3')\"
    [[ -d \"\$(_build_lease_dir service)\" ]] && echo LEASE_HELD_DURING_BUILD=1
    release_props_restore_and_release '$props3' \"\$snap\" service
    echo \"FINAL=\$(cat '$props3')\"
    [[ -d \"\$(_build_lease_dir service)\" ]] && echo LEASE_STILL_HELD_AFTER=1 || echo LEASE_RELEASED_AFTER=1
" 2>&1)"
rc3=$?
if [[ $rc3 -eq 0 ]]; then ok "stamp+restore round trip exits 0"; else bad "round trip failed (rc $rc3): $out3"; fi
if [[ "$out3" == *"MID=release_version=9.9.9"* ]]; then ok "file carries the stamp while the lease is held"; else bad "stamp not visible mid-hold: $out3"; fi
if [[ "$out3" == *"LEASE_HELD_DURING_BUILD=1"* ]]; then ok "lease directory exists during the held window"; else bad "lease not held during the window: $out3"; fi
if [[ "$out3" == *"FINAL=release_version=$(printf '%s\n' "$baseline3" | sed -n '1s/^[^=]*=//p')"* || "$out3" == *"FINAL=$baseline3"* ]]; then
    ok "file restored to its pre-stamp bytes"
else
    bad "file not restored to baseline: $out3 (baseline was: $baseline3)"
fi
if [[ "$out3" == *"LEASE_RELEASED_AFTER=1"* ]]; then ok "lease released after restore"; else bad "lease still held after restore_and_release: $out3"; fi
if [[ ! -d "$leasedir3" ]]; then ok "lease directory gone on disk after the round trip"; else bad "lease directory still present on disk"; fi
if [[ "$(cat "$props3")" == "$baseline3" ]]; then ok "final on-disk bytes equal the original baseline"; else bad "final bytes differ from baseline: $(cat "$props3")"; fi

# ── Test 4: guard refuses and releases the lease again, never leaves it ──
# held. It does NOT skip acquiring: a dirty file while nobody legitimately
# holds the lease is exactly the abandoned-stamp case this whole fix is
# for, and that can only be told apart from "a live holder is mid-stamp"
# by actually holding the lease ourselves and checking (see the "no
# pre-acquire guard" comment in release_props_stamp_under_lease itself).
echo "Test 4: stamp_under_lease on an already-dirty tree refuses and releases the lease again"
repo4="$WORKDIR/repo4"
_fake_repo "$repo4"
props4="$repo4/$PROPS_RELPATH"
printf 'release_version=stale-alien-stamp\nbuild_ref=someone-elses\n' > "$props4"
out4="$(bash -c "source '$repo4/scripts/lib/release-props-lease.sh'; release_props_stamp_under_lease '$props4' service 2 release_version=9.9.9" 2>&1)"
rc4=$?
if [[ $rc4 -eq 75 ]]; then ok "stamp_under_lease on a dirty tree refuses (rc 75)"; else bad "stamp_under_lease on a dirty tree returned rc $rc4 (expected 75): $out4"; fi
leasedir4="$(_lease_dir_for "$repo4" service)"
if [[ ! -d "$leasedir4" ]]; then
    ok "the lease is not left held after a refused stamp"
else
    bad "the lease is still held after stamp_under_lease refused"
fi
if [[ "$(cat "$props4")" == "release_version=stale-alien-stamp"* ]]; then
    ok "the alien stamp is untouched by the refused call"
else
    bad "stamp_under_lease mutated the file despite refusing: $(cat "$props4")"
fi

# ── Test 5: the actual incident — two stampers, bad interleaving ─────────
# Reproduces nexus-iexvl directly: stamper A (models the pre-fix
# migration-rehearsal run.sh) takes a snapshot and stamps WITHOUT ever
# holding the lease, holds the stamp for a long "run", and restores its
# OWN snapshot only at its own end. Stamper B (models scripts/build-
# gate-jar.sh, POST-fix, using this library) starts partway through A's
# window and must not be able to leave the tree dirty when it is done,
# REGARDLESS of what A is doing — the guard has to catch A's alien stamp
# before B ever touches the lease, rather than B blindly backing up
# whatever bytes happen to be on disk (the actual defect: build-gate-
# jar.sh's plain `cp "$props" "$backup"` had no such check).
echo "Test 5: nexus-iexvl reproduction — B (fixed) never adopts A's (unguarded) stamp"
repo5="$WORKDIR/repo5"
_fake_repo "$repo5"
props5="$repo5/$PROPS_RELPATH"
baseline5="$(cat "$props5")"

# Stamper A: the OLD, buggy shape — no lease at all around the stamp.
(
    printf 'release_version=0.1.117\nbuild_ref=A-run-stamp\n' > "$props5"
    sleep 2
    printf '%s' "$baseline5" > "$props5"
) &
stamper_a=$!

# Give A a moment to land its stamp before B starts (models B starting
# "partway through" A's run, per the incident).
sleep 0.3

out5="$(bash -c "
    source '$repo5/scripts/lib/release-props-lease.sh'
    snap=\$(release_props_stamp_under_lease '$props5' service 5 release_version=9.9.9 build_ref=B-run-stamp)
    rc=\$?
    if [[ \$rc -ne 0 ]]; then echo \"B_REFUSED rc=\$rc\"; exit 0; fi
    sleep 0.2
    release_props_restore_and_release '$props5' \"\$snap\" service
    echo \"B_COMPLETED\"
" 2>&1)"

wait "$stamper_a"

if [[ "$out5" == *"B_REFUSED"* ]]; then
    ok "B correctly refused rather than adopting A's alien stamp (guard caught it)"
elif [[ "$out5" == *"B_COMPLETED"* ]]; then
    ok "B ran to completion (the lease serialized B strictly after A finished)"
else
    bad "B neither refused nor completed cleanly: $out5"
fi
final5="$(cat "$props5")"
if [[ "$final5" == "$baseline5" ]]; then
    ok "tree is clean (equals baseline) after both stampers finish — the nexus-iexvl leftover-stamp defect does not reproduce"
else
    bad "tree left dirty after both stampers finished: $final5 (this IS the nexus-iexvl defect reproducing)"
fi

# ── Test 6: two COMPLIANT stampers via this library never interleave badly ─
# Both sides use release_props_stamp_under_lease + restore_and_release (the
# shape every writer is being moved to). Neither may ever observe the
# other's stamp, and the tree must be clean once both are done -- this is
# the direct proof that the shared primitive, used as intended by two
# concurrent callers, cannot reproduce nexus-iexvl.
echo "Test 6: two lease-compliant stampers via this library never leave a stale stamp"
repo6="$WORKDIR/repo6"
_fake_repo "$repo6"
props6="$repo6/$PROPS_RELPATH"
baseline6="$(cat "$props6")"

_compliant_stamper() {
    local repo="$1" stamp="$2" sleep_s="$3" out_file="$4"
    bash -c "
        source '$repo/scripts/lib/release-props-lease.sh'
        snap=\$(release_props_stamp_under_lease '$props6' service 10 release_version=$stamp build_ref=$stamp) || { echo \"REFUSED rc=\$?\" > '$out_file.mid'; exit 0; }
        cat '$props6' > '$out_file.mid'
        sleep $sleep_s
        release_props_restore_and_release '$props6' \"\$snap\" service
        echo DONE > '$out_file.done'
    "
}

obs_c="$WORKDIR/obs_c"
obs_d="$WORKDIR/obs_d"
_compliant_stamper "$repo6" "C-STAMP" 1 "$obs_c" &
stamper_c=$!
sleep 0.1
_compliant_stamper "$repo6" "D-STAMP" 0 "$obs_d" &
stamper_d=$!
wait "$stamper_c" "$stamper_d"

if [[ -f "$obs_c.mid" && "$(cat "$obs_c.mid")" == *"C-STAMP"* ]]; then
    ok "stamper C observed only its own stamp while holding the lease"
else
    bad "stamper C did not observe a clean view of its own stamp: $(cat "$obs_c.mid" 2>/dev/null)"
fi
if [[ -f "$obs_d.mid" && "$(cat "$obs_d.mid")" == *"D-STAMP"* ]]; then
    ok "stamper D observed only its own stamp while holding the lease (never C's, never a blank clobber)"
else
    bad "stamper D did not observe a clean view of its own stamp: $(cat "$obs_d.mid" 2>/dev/null)"
fi
final6="$(cat "$props6")"
if [[ "$final6" == "$baseline6" ]]; then
    ok "tree is back to baseline after both compliant stampers finish"
else
    bad "tree left dirty after both compliant stampers finished: $final6"
fi
leasedir6="$(_lease_dir_for "$repo6" service)"
if [[ ! -d "$leasedir6" ]]; then
    ok "lease released after both compliant stampers finish"
else
    bad "lease directory still present after both compliant stampers finished"
fi

echo
echo "release-props-lease_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
