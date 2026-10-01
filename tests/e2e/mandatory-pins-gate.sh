#!/usr/bin/env bash
# The GitHub-backed `mandatory_regression_pin` tests, run where gh auth exists (nexus-z0o2p.41).
#
# These pins prove the release-gate scripts (check_release_ci_evidence.py,
# check_engine_release_floor.py) against the real thing: live check-runs, live branch
# protection, real git tags. They need `gh` authenticated, so they cannot run under the
# local-service gate's fenced HOME, which never mirrors ~/.config/gh (see
# tests/e2e/lib/mandatory_pins.sh for the decision and the reason). They are `integration`
# marked, so the default `uv run pytest` selection excludes them too: this script is where
# they run.
#
#   tests/e2e/mandatory-pins-gate.sh
#
# Runs in the operator's REAL HOME (no fence, no scratch config dir): that is the whole point.
# NX_MANDATORY_PIN_SKIP_BUDGET defaults to 0, so a skipped pin fails the run, in conftest's
# session guard and again in the junit read below. CI's job token cannot read branch protection,
# so the nightly workflow sets the budget to 1 for exactly that one pin; nothing else raises it.
#
# NON-VACUITY: the run must report EXACTLY MANDATORY_PIN_EXPECTED pin tests (collected before
# and counted after), skip at most the budget, and run at least one.
#
# Ends `MANDATORY PINS GATE PASSED` or `MANDATORY PINS GATE FAILED`.
set -uo pipefail
export NX_NO_TELEMETRY=1
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 2
# shellcheck source=lib/mandatory_pins.sh
. "$REPO_ROOT/tests/e2e/lib/mandatory_pins.sh"

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/nx-pins.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
BUDGET="${NX_MANDATORY_PIN_SKIP_BUDGET:-0}"
export NX_MANDATORY_PIN_SKIP_BUDGET="$BUDGET"

fail() { echo "[pins] $*" >&2; echo "MANDATORY PINS GATE FAILED"; exit 1; }

# These tests drive `gh` and `git`, not the engine; the substrate would only take the machine
# suite lease for nothing (the lsg collect counts do the same).
export NX_TEST_T2_SUBSTRATE=none

COLLECTED="$(uv run pytest -o addopts="" -m "$MANDATORY_PIN_MARK_EXPR" --collect-only -q 2>/dev/null | grep -cE '::' || true)"
[ "$COLLECTED" -eq "$MANDATORY_PIN_EXPECTED" ] \
  || fail "collected $COLLECTED mandatory_regression_pin test(s), expected exactly $MANDATORY_PIN_EXPECTED (a new pin must bump MANDATORY_PIN_EXPECTED in tests/e2e/lib/mandatory_pins.sh)"

uv run pytest -o addopts="" -m "$MANDATORY_PIN_MARK_EXPR" -q -rs --color=no --junit-xml="$SCRATCH/pins.xml"
PYTEST_RC=$?
[ "$PYTEST_RC" -eq 0 ] || fail "pytest exited $PYTEST_RC"

python3 "$REPO_ROOT/tests/e2e/lib/mandatory_pins_check.py" "$SCRATCH/pins.xml" "$MANDATORY_PIN_EXPECTED" "$BUDGET" \
  || fail "the junit read found the pins did not all run (see the MANDATORY PINS line above)"
echo "MANDATORY PINS GATE PASSED"
