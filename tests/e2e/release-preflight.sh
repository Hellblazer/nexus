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

# `cmd | head -1` pipes a producer into an early-exit consumer, which
# tests/test_pipefail_early_exit_consumer_lint.py forbids under `set -o
# pipefail`: head can exit before the producer finishes, masking its status
# (or SIGPIPE-ing it). This script is NEW, so it takes the clean shape rather
# than joining that lint's exemption list -- capture the whole value, then
# slice the first line with a parameter expansion. No pipe, no early exit.
first_line () { printf '%s' "${1%%$'\n'*}"; }

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
    local detail stripped
    stripped="$(printf '%s' "$out" | sed -e 's/\x1b\[[0-9;]*m//g')"
    detail="$(printf '%s' "$stripped" \
      | grep -aE '^(FAILED|E  +|FATAL)|GATE (FAILED|UNVERIFIABLE|BLOCKED)|assert' \
      | sed -e 's/^E  *//' | cut -c1-160)"
    # A red whose reason matched no pattern above used to record an EMPTY
    # detail -- the reader gets "[FAIL] engine-release-floor" and nothing
    # else, and has to re-run the leg by hand to learn why. Measured at the
    # 7.27.0 cut: check_engine_release_floor.py's exit 3 ("TRACKER NOT
    # RECORDED (exit 3): ...") names its own remedy in one line and matched
    # nothing. Second tier: this repo's loud-verdict shape -- a line opening
    # with an ALL-CAPS label followed by "(" or ":". Deliberately NOT a
    # last-non-empty-line fallback: these gates interleave PASSING lines
    # after the failing one, so last-line attributed the red to a green
    # ("engine source is current") -- a wrong reason is worse than none.
    if [ -z "$detail" ]; then
      detail="$(printf '%s' "$stripped" \
        | grep -aE '^[A-Z][A-Z0-9 ]{2,}[(:]' | cut -c1-160)"
      detail="$(first_line "$detail")"
    fi
    [ -n "$detail" ] || detail="(no verdict line matched -- re-run this leg alone to see its output)"
    record FAIL "$name" "$(first_line "$detail")"
  fi
}

# Step 0: refresh remote refs. check_engine_release_floor's pin-currency and
# source-ancestry arms read LOCAL `git tag -l`, and the snapshot replay below
# reads origin/main. release.yml checks out with fetch-depth:0, so CI sees
# origin's refs. An engine tag published since your last fetch therefore
# GREENS locally and REDS AT PUBLISH -- where the tree is frozen at the tag
# and a same-tag re-run reads the identical tree, so the remedy is a whole new
# tag rather than a retry. Cheap insurance against an expensive, un-retryable
# class.
git fetch --tags --force --quiet origin 2>/dev/null || echo "  [warn] could not fetch remote refs -- tag/branch checks may be stale"

echo "== release preflight =="

# 1. Engine identity: pin vs newest published tag vs live cloud vs source drift.
#    A PAIRED release (release skill Step 0) has the cloud BEHIND the floor by
#    construction until the deploy fires at client-tag push, so the bare form
#    is red on every paired cut (measured: 7.23.0, 2026-08-29). With
#    NX_PAIRED_DEPLOY=engine-service-vX.Y.Z set, this runs the SAME mechanized
#    paired acceptance the human Step 0 invocation uses (--paired-deploy: the
#    named tag must verify published, pinned, newest) -- stricter, never looser,
#    and the flag names the pairing out loud in the transcript.
# CLOUD-MODE BOX: check_engine_release_floor.py's bare form exits 3 ("TRACKER
# NOT RECORDED") by design (nexus-nx3l5) until it is told where conexus's
# STEP-6 gate reports live. Export NX_GATE_REPORT_DIR=<conexus checkout>/deploy
# before running this script, or the floor leg reds for a reason that is not a
# release problem. Local-mode boxes need nothing.
if [ -n "${NX_PAIRED_DEPLOY:-}" ]; then
    check "engine-release-floor"  uv run python scripts/check_engine_release_floor.py --paired-deploy "$NX_PAIRED_DEPLOY"
else
    check "engine-release-floor"  uv run python scripts/check_engine_release_floor.py
fi
# 2. Merge-blocking on EVERY PR to main -- an unacknowledged ## Unshipped entry
#    blocks the release PR itself, not just the tag.
check "wire-contract-ledger"      uv run python scripts/check_engine_release_floor.py --ledger-only
# (3 and 4, the remediation-commit gate and its snapshot replay, were RETIRED
#  with nexus-2zmfw -- see that bead. Numbering below is left as-is.)
# 5. Seven version surfaces + both source.ref fields + the emptied ledger.
# plugin-drift-ledger.yml runs this with NX_REQUIRE_PLUGIN_DRIFT_CHECK=1 and
# NX_TEST_T2_SUBSTRATE=none, which turns a SKIP into a failure. Running it bare
# here would let a skip pass locally and differ from CI.
check "version-parity+ledger"     env NX_REQUIRE_PLUGIN_DRIFT_CHECK=1 NX_TEST_T2_SUBSTRATE=none \
                                    uv run pytest tests/test_plugin_structure.py tests/test_plugin_release_drift_ledger.py -q
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
  cur="$(first_line "$(sed -n 's/^REQUIRED_ENGINE_VERSION[^(]*(\([0-9]*\), *\([0-9]*\), *\([0-9]*\)).*/\1.\2.\3/p' src/nexus/engine_version.py)")"
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
  nid="$(first_line "$(grep -oE 'tests/[A-Za-z0-9_/]+\.py::[A-Za-z0-9_]+' tests/e2e/release-sandbox.sh)")"
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
      lived_in)   exp="$(first_line "$(sed -n 's/^LIVED_IN_EXPECTED=\([0-9]*\).*/\1/p'   tests/e2e/local-service-gate.sh)")" ;;
      cloud_mode) exp="$(first_line "$(sed -n 's/^CLOUD_MODE_EXPECTED=\([0-9]*\).*/\1/p' tests/e2e/local-service-gate.sh)")" ;;
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

# 13. THE ONE THAT WOULD HAVE BLOCKED THE 7.15.0 PR. `-m lint` is deselected
#     by the default addopts, so NO local battery step runs it -- but ci.yml's
#     test-lint feeds pytest-gate, which is main's required check. A line-pinned
#     exemption list in test_pipefail_early_exit_consumer_lint.py restale-izes
#     on ANY edit that shifts lines in a covered script, so this reds from
#     ordinary edits, not just from new violations. ~2 min: by far the most
#     expensive item here, and admitted anyway because the alternative is
#     discovering it as a failed required check on the release PR.
#     NO_COLOR is load-bearing: the floor parser anchors its summary regex at
#     end-of-line, and an ANSI reset after the duration makes it read 0
#     executed and trip on a perfectly good run.
lint_leg_check () {
  local out plain
  out="$(NO_COLOR=1 uv run pytest -m lint -q 2>&1)"
  plain="$(printf '%s' "$out" | sed -e 's/\x1b\[[0-9;]*m//g')"
  # No `| grep -q` here: that is the very pattern this leg lints for. Match
  # against the captured variable instead, exactly as the lint's own remedy
  # text prescribes.
  [[ "$plain" =~ ([0-9]+)\ passed ]] || { printf '%s\n' "$plain" | tail -3; return 1; }
  printf '%s' "$plain" > "${TMPDIR:-/tmp}/nx-preflight-lint.txt"
  uv run python scripts/check_lint_leg_non_vacuity.py "${TMPDIR:-/tmp}/nx-preflight-lint.txt" || return 1
}
check "lint-leg+floor"            lint_leg_check

# 14. ci.yml's test-mode-census job, also merge-blocking via pytest-gate. The
#     session-items census skips under `-n auto` (each xdist worker sees a
#     partial view), so the fast local loop cannot fire it -- exactly how a
#     mode-lint violation reached this branch in the first place. 20s.
check "mode-census"               env NX_CENSUS_ONLY_JOB=1 NX_SCENARIO_SKIP_BUDGET=999 NX_TEST_T2_SUBSTRATE=none \
                                    uv run pytest tests/ -q

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
