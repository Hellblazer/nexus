#!/usr/bin/env bash
# Release PREFLIGHT: every seconds-scale, deterministic release blocker, run
# FIRST and run ALL — never abort on the first red. The ONE preflight (cleanup
# step 11, nexus-0r1uz): the former pin-sweep script is folded in here.
#
# WHY THIS EXISTS (2026-08-22, the 7.15.0 cut). The battery was ordered
# expensive-legs-first and abort-on-first-red, so each blocker cost a full
# replay of everything before it and hid every other blocker behind it. One
# blocker that day was a pure assertion costing well under a second:
# REQUIRED_CHECK_CONTEXTS drift vs main's live branch protection, found 13.5
# minutes into local-service-gate, at the very end of its run. Cheap checks
# first, all of them, collecting every failure, before a single expensive leg
# starts. A red here costs seconds; the same red found downstream costs an hour
# and masks its siblings.
#
# THE CHECKS:
#   wire-contract-ledger   check_engine_release_floor.py --ledger-only (merge-blocking on every PR to main)
#   ci-evidence-contexts   the required-context drift tests, run with `-m ""` so the live check executes
#   mandatory-pins         the GitHub-backed mandatory_regression_pin tests, under the operator's real HOME
#   pin sweep              lint bucket (+ non-vacuity floor), the non-lint pin tests, wire-contract
#                          pairing, ruff over src (the CI lint job scope)
#
# ADMISSION CRITERIA -- a check belongs here only if it is genuinely release-
# BLOCKING and deterministic (no ambient service, no sandbox HOME). Anything
# slower stays in the battery proper. This file must never grow into a second
# battery.
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
    # detail -- the reader gets "[FAIL] wire-contract-ledger" and nothing
    # else, and has to re-run the leg by hand to learn why. Second tier: this repo's loud-verdict shape -- a line opening
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

# Step 0: refresh remote refs. ci-evidence reads origin/main's live branch
# protection and the ledger check reads tags; release.yml checks out with
# fetch-depth:0, so CI sees origin's refs. A tag published since your last
# fetch therefore GREENS locally and REDS AT PUBLISH -- where the tree is
# frozen at the tag and a same-tag re-run reads the identical tree, so the
# remedy is a whole new tag rather than a retry.
git fetch --tags --force --quiet origin 2>/dev/null || echo "  [warn] could not fetch remote refs -- tag/branch checks may be stale"

echo "== release preflight =="

# 1. Merge-blocking on EVERY PR to main -- an unacknowledged ## Unshipped entry
#    blocks the release PR itself, not just the tag.
check "wire-contract-ledger"      uv run python scripts/check_engine_release_floor.py --ledger-only
# 2. THE 13.5-MINUTE ONE. Markers disabled so the integration-marked live
#    branch-protection drift test actually RUNS -- under default selection it
#    is deselected and this proves nothing.
check "ci-evidence-contexts"      uv run pytest tests/scripts/test_check_release_ci_evidence.py -q -m ""

# 3. The GitHub-backed `mandatory_regression_pin` tests (nexus-z0o2p.41), run
#    here under the operator's REAL HOME where gh auth exists. They need `gh`
#    authenticated, so they cannot run under the local-service gate's fenced
#    HOME (which never mirrors ~/.config/gh; see tests/e2e/lib/mandatory_pins.sh
#    for the decision and the reason). They are `integration` marked, so the
#    default selection excludes them too. NX_MANDATORY_PIN_SKIP_BUDGET defaults
#    to 0, so a skipped pin fails the run in conftest's session guard. The count
#    is asserted EXACT before the run: a new pin bumps MANDATORY_PIN_EXPECTED.
#    The suite runs every test under a throwaway HOME, so a `gh` login that
#    lives in the real HOME's config is invisible to `gh auth token` inside
#    pytest; the token is resolved here and handed over in the environment
#    variable the pins read first. It is never printed.
mandatory_pins_run () {
  # shellcheck source=lib/mandatory_pins.sh disable=SC1091
  . tests/e2e/lib/mandatory_pins.sh
  local collected
  if [ -z "${GITHUB_TOKEN:-}" ]; then
    if [ -n "${GH_TOKEN:-}" ]; then
      GITHUB_TOKEN="$GH_TOKEN"
    elif command -v gh >/dev/null 2>&1; then
      GITHUB_TOKEN="$(gh auth token 2>/dev/null || true)"
    fi
    [ -z "${GITHUB_TOKEN:-}" ] || export GITHUB_TOKEN
  fi
  export NX_TEST_T2_SUBSTRATE=none NX_MANDATORY_PIN_SKIP_BUDGET="${NX_MANDATORY_PIN_SKIP_BUDGET:-0}"
  collected="$(uv run pytest -o addopts="" -m "$MANDATORY_PIN_MARK_EXPR" --collect-only -q 2>/dev/null | grep -cE '::' || true)"
  [ "$collected" -eq "$MANDATORY_PIN_EXPECTED" ] \
    || { echo "FATAL: collected $collected mandatory_regression_pin test(s), expected exactly $MANDATORY_PIN_EXPECTED (a new pin must bump MANDATORY_PIN_EXPECTED in tests/e2e/lib/mandatory_pins.sh)"; return 1; }
  uv run pytest -o addopts="" -m "$MANDATORY_PIN_MARK_EXPR" -q -rs --color=no
}
check "mandatory-pins"            mandatory_pins_run

# 4. The pin sweep: every cheap ratchet, ledger, parity and reference-rot check
#    this repo carries. `-m lint` is deselected by the default addopts, so NO
#    local battery step runs it -- but ci.yml's test-lint feeds pytest-gate,
#    which is main's required check. A line-pinned exemption list in
#    test_pipefail_early_exit_consumer_lint.py restale-izes on ANY edit that
#    shifts lines in a covered script, so this reds from ordinary edits, not
#    just from new violations. ~2 min: by far the most expensive item here,
#    and admitted anyway because the alternative is discovering it as a failed
#    required check on the release PR.
#    NO_COLOR is load-bearing: the floor parser anchors its summary regex at
#    end-of-line, and an ANSI reset after the duration makes it read 0
#    executed and trip on a perfectly good run.
lint_leg_check () {
  local out plain
  out="$(NO_COLOR=1 uv run pytest -m lint -q 2>&1)"
  plain="$(printf '%s' "$out" | sed -e 's/\x1b\[[0-9;]*m//g')"
  # No `| grep -q` here: that is the very pattern this leg lints for. Match
  # against the captured variable instead, exactly as the lint's own remedy
  # text prescribes.
  [[ "$plain" =~ ([0-9]+)\ passed ]] || { printf '%s\n' "$plain" | tail -3; return 1; }
  # Non-vacuity floor (nexus-wixar): a mass-skip exits 0, so read the PASSED
  # count off the summary and fail below 400 (ci.yml's test-lint step, same floor).
  [ "${BASH_REMATCH[1]}" -ge 400 ] || { echo "lint leg passed only ${BASH_REMATCH[1]} tests (floor 400) -- a mass-skip"; return 1; }
}
check "lint-leg+floor"            lint_leg_check
#    The pin tests that are NOT lint-marked (engine version, release ledgers,
#    PG bundle parity, deadline ordering).
check "pin-tests"                 uv run pytest -q -p no:cacheprovider \
                                    tests/test_engine_version.py tests/test_plugin_release_drift_ledger.py \
                                    tests/test_ci_release_ledger_gate.py tests/test_pg_bundle_version_parity.py \
                                    tests/test_embed_deadline_default_ordering.py
check "wire-contract-pairing"     uv run python scripts/check_wire_contract_pairing.py
check "ruff-src"                  uv run ruff check src

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
