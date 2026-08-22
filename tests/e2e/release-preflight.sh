#!/usr/bin/env bash
# Release PREFLIGHT: every seconds-scale, deterministic release blocker, run
# FIRST and run ALL — never abort on the first red.
#
# WHY THIS EXISTS (2026-08-22, the 7.15.0 cut). The battery was ordered
# expensive-legs-first and abort-on-first-red, so each blocker cost a full
# replay of everything before it and hid every other blocker behind it. Two
# blockers that day were pure assertions costing well under a second:
#
#   * REQUIRED_CHECK_CONTEXTS drift vs main's live branch protection -- found
#     13.5 minutes into local-service-gate, at the very end of its run.
#   * The --package-upgrade PREV_ENGINE_TAG staleness guard -- found 20
#     minutes in, after smoke + shakeout + LSG + fresh-install-mvv had all
#     re-run, because it is a guard clause in the first seconds of run.sh.
#
# Each was a one-line fix behind an hour of waiting, discovered one at a time.
# So: cheap checks first, all of them, collecting every failure, before a
# single expensive leg starts. A red here costs seconds; the same red found
# downstream costs an hour and masks its siblings.
#
# ADMISSION CRITERIA -- a check belongs here only if it is:
#   1. seconds-scale (no Docker, no suite, no network install),
#   2. deterministic (no ambient service, no sandbox HOME), and
#   3. genuinely release-BLOCKING (its red stops the cut).
# Anything slower stays in the battery proper. This file must never grow into
# a second battery: its whole value is that it finishes before you look away.
#
# NON-VACUITY: a check whose dependency is absent reports SKIP and the run
# ends UNVERIFIED (exit 2), never PASSED. "Could not check" is not "fine".
set -uo pipefail
cd "$(dirname "$0")/../.."

PASS=0; FAIL=0; SKIP=0
declare -a RESULTS=()

record () { # status, name, detail
  RESULTS+=("$1|$2|$3")
  case "$1" in
    PASS) PASS=$((PASS+1)) ;;
    FAIL) FAIL=$((FAIL+1)) ;;
    SKIP) SKIP=$((SKIP+1)) ;;
  esac
  printf '  [%s] %s%s\n' "$1" "$2" "${3:+ -- $3}"
}

check () { # name, command...
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    record PASS "$name" ""
  else
    # Strip ANSI first: pytest colourises source context, and an un-stripped
    # grep happily returns a highlighted SOURCE line instead of the assertion.
    # The detail line is what the next person reads -- it must name the cause.
    record FAIL "$name" "$(printf '%s' "$out" \
      | sed -e 's/\x1b\[[0-9;]*m//g' \
      | grep -aE '^(FAILED|E  +|FATAL)|GATE (FAILED|UNVERIFIABLE|BLOCKED)|assert' \
      | head -1 | sed -e 's/^E  *//' | cut -c1-160)"
  fi
}

echo "== release preflight =="

# 1. Engine identity: pin vs newest published tag vs live cloud vs source drift.
check "engine-release-floor"      uv run python scripts/check_engine_release_floor.py
# 2. Merge-blocking on EVERY PR to main -- an unacknowledged ## Unshipped entry
#    blocks the release PR itself, not just the tag.
check "wire-contract-ledger"      uv run python scripts/check_engine_release_floor.py --ledger-only
# 3. Remediation beads whose fix must ride this release.
check "remediation-commits"       uv run python scripts/check_remediation_commits_ride_release.py --release-ref HEAD
# 4. The tag-publish snapshot replay, against the base a merge commit really
#    has (main's tip), not the local HEAD^ -- see --release-base-ref's help.
check "remediation-snapshot"      uv run python scripts/check_remediation_commits_ride_release.py \
                                    --release-ref HEAD --release-base-ref origin/main \
                                    --bd-export-json .release-gates/remediation-snapshot.json --verify-snapshot
# 5. Seven version surfaces + both source.ref fields + the emptied ledger.
check "version-parity+ledger"     uv run pytest tests/test_plugin_structure.py tests/test_plugin_release_drift_ledger.py -q
# 6. THE 13.5-MINUTE ONE. Markers disabled so the integration-marked live
#    branch-protection drift test actually RUNS -- under default selection it
#    is deselected and this proves nothing.
check "ci-evidence-contexts"      uv run pytest tests/scripts/test_check_release_ci_evidence.py -q -m ""

# 7. THE 20-MINUTE ONE. Source run.sh's derivation and apply the same staleness
#    predicate its guard uses, without provisioning anything.
prev_stale_check () {
  eval "$(sed -n '/^_engine_tuple_at_release()/,/^}/p;/^_derive_prev_release()/,/^}/p;/^_derive_prev_engine_tag()/,/^}/p' \
          tests/e2e/migration-rehearsal/run.sh)"
  local rel tag cur
  rel="$(_derive_prev_release)" || return 1
  tag="$(_derive_prev_engine_tag "$rel")" || return 1
  cur="$(sed -n 's/^REQUIRED_ENGINE_VERSION[^(]*(\([0-9]*\), *\([0-9]*\), *\([0-9]*\)).*/\1.\2.\3/p' src/nexus/engine_version.py | head -1)"
  [ -n "$cur" ] || { echo "FATAL: cannot read REQUIRED_ENGINE_VERSION"; return 1; }
  if [ "$tag" = "engine-service-v$cur" ]; then
    echo "FATAL: PREV_ENGINE_TAG ($tag) equals REQUIRED_ENGINE_VERSION ($cur) -- --package-upgrade would refuse as vacuous"
    return 1
  fi
  echo "prev=$rel engine=$tag current=$cur"
}
check "pkg-upgrade-staleness"     prev_stale_check

# 9-12 are RELOCATIONS, not new lints: each is an existing gate predicate that
# already fails the battery, just far too late. Sourced/derived from the gate
# scripts themselves so the two cannot drift apart.

# 9. release-sandbox.sh:851 FIXTURE_FILES staleness -- 36 hard-coded repo paths
#    `test -f`-ed at shakedown step 2/11, roughly 40 minutes into the battery
#    and inside its LAST leg, so a trip re-runs the whole shakedown. Extracting
#    the array from the script keeps one source of truth.
fixture_files_check () {
  local arr n missing=0 f
  arr="$(sed -n '/^        FIXTURE_FILES=(/,/^        )/p' tests/e2e/release-sandbox.sh)"
  [ -n "$arr" ] || { echo "FATAL: could not extract FIXTURE_FILES from release-sandbox.sh"; return 1; }
  eval "$arr"
  n=${#FIXTURE_FILES[@]}
  [ "$n" -gt 0 ] || { echo "FATAL: FIXTURE_FILES extracted empty -- parser drift"; return 1; }
  for f in "${FIXTURE_FILES[@]}" tests/fixtures/tc-sql.pdf tests/fixtures/bft-to-smr.pdf; do
    [ -f "$f" ] || { echo "FATAL: shakedown fixture missing from repo: $f"; missing=1; }
  done
  [ "$missing" -eq 0 ] || return 1
  echo "$n fixture files + 2 pdfs present"
}
check "shakedown-fixtures"        fixture_files_check

# 10. release-sandbox.sh:1040 pins a pytest node id by hand; a rename makes
#     pytest exit 4/5 and the branch hard-exits at shakedown step 5/11 --
#     past the repo index, BOTH pdf indexes (incl. the cold MinerU download)
#     and the RDR index.
nodeid_check () {
  local nid
  nid="$(grep -oE 'tests/[A-Za-z0-9_/]+\.py::[A-Za-z0-9_]+' tests/e2e/release-sandbox.sh | head -1)"
  [ -n "$nid" ] || { echo "FATAL: could not extract the pinned node id from release-sandbox.sh"; return 1; }
  uv run pytest --collect-only -q -m "" "$nid" >/dev/null 2>&1 \
    || { echo "FATAL: pinned node id does not collect: $nid"; return 1; }
  echo "$nid collects"
}
check "shakedown-nodeid"          nodeid_check

# 11. local-service-gate.sh:599/:634 marker carve-out EXACT counts. Highest
#     likelihood on the list -- LIVED_IN_EXPECTED moved 39 -> 41 -> 42 inside
#     one month, and test_lived_in_marker_registration.py explicitly disclaims
#     enforcing it ("the behavioral bound lives in the gate script itself").
#     Currently aborts ~3-6 min in, but before LSG's 13.5-minute pytest leg.
#     COST: ~16s of this preflight's runtime, the largest single item. Kept
#     deliberately -- it is still 16 seconds against a 6-minute rediscovery.
marker_counts_check () {
  local m exp act
  for m in lived_in cloud_mode; do
    case "$m" in
      lived_in)   exp="$(sed -n 's/^LIVED_IN_EXPECTED=\([0-9]*\).*/\1/p'   tests/e2e/local-service-gate.sh | head -1)" ;;
      cloud_mode) exp="$(sed -n 's/^CLOUD_MODE_EXPECTED=\([0-9]*\).*/\1/p' tests/e2e/local-service-gate.sh | head -1)" ;;
    esac
    [ -n "$exp" ] || { echo "FATAL: could not read the expected count for $m"; return 1; }
    act="$(uv run pytest -m "integration and $m" --collect-only -q 2>/dev/null | grep -cE '::' || true)"
    [ "$act" -eq "$exp" ] || { echo "FATAL: $m carve-out is $act tests, gate expects exactly $exp"; return 1; }
  done
  echo "lived_in + cloud_mode counts match the gate"
}
check "marker-carveout-counts"    marker_counts_check

# 12. uv.lock freshness -- `uv lock --check` is run.sh:639's `uv export
#     --locked` predicate at 0.02s. Mostly shadowed by item 5, which pins
#     uv.lock's conexus version; this covers an unlocked NEW dependency.
check "uv-lock-fresh"             uv lock --check

# 8. Docker is a hard dependency of the migration-rehearsal legs. Absent Docker
#    is a SKIP that makes the whole preflight UNVERIFIED, never a pass.
if docker info >/dev/null 2>&1; then
  record PASS "docker-available" ""
else
  record SKIP "docker-available" "migration-rehearsal legs cannot run"
fi

echo
echo "== preflight summary: $PASS passed, $FAIL failed, $SKIP skipped =="
if [ "$FAIL" -gt 0 ]; then
  echo "PREFLIGHT FAILED -- fix ALL of the above before starting the expensive battery:"
  for r in "${RESULTS[@]}"; do
    [ "${r%%|*}" = "FAIL" ] || continue
    r="${r#FAIL|}"
    printf '  - %s: %s\n' "${r%%|*}" "${r#*|}"
  done
  exit 1
fi
if [ "$SKIP" -gt 0 ]; then
  echo "PREFLIGHT UNVERIFIED -- a dependency was absent; 'could not check' is not 'fine'."
  exit 2
fi
echo "PREFLIGHT PASSED"
