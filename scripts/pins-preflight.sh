#!/usr/bin/env bash
# Front-loaded pin sweep: every cheap ratchet, ledger, parity and reference-rot
# check this repo carries, run together BEFORE any hour-long gate, continuing
# past failures so every stale pin surfaces in ONE report instead of one per
# gate run. Runs in minutes. Exit 1 if any leg is red, 2 if a leg was vacuous.
#
# Legs:
#   1. the lint bucket (-m lint: reference rot, wire-pairing lint, plugin
#      structure, hook drift, marker coverage, shell exemption line keys)
#   2. the pin tests that are NOT lint-marked (engine version, release ledgers,
#      PG bundle parity, deadline ordering)
#   3. scripts/check_wire_contract_pairing.py
#   4. ruff over src (the CI lint job scope; tests are not ruff-gated)
set -u -o pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2
reds=()
run() {
  local name="$1"; shift
  echo "== $name"
  if "$@"; then echo "   ok: $name"; else echo "   RED: $name"; reds+=("$name"); fi
}

# Classify a captured `pytest -m lint` run from its OWN summary line only,
# never a substring grep across the whole captured output (nexus-aut8g): a
# transient line elsewhere in the run -- a substrate sweep warning, a
# captured sub-log -- can contain "N errors " or "N failed " and reds a
# genuinely clean sweep. Pytest's own final summary line is the only line
# that reports "in N.NNs" elapsed time, so it is found by grepping for
# that shape and taking the LAST match (pytest prints exactly one such
# line per run, at the end); only THAT line is then checked for a real
# failure/error count. Prints "PASS <line>" / "RED <line-or-reason>" /
# "VACUOUS <line-or-reason>" on stdout.
_lint_bucket_verdict() {
  local outfile="$1" summary
  summary="$(grep -E '\bin [0-9]+\.[0-9]+s\b' "$outfile" | tail -1)"
  if [[ -z "$summary" ]]; then
    echo "RED no pytest summary line found in the captured output"
    return
  fi
  if grep -qE '[0-9]+ (errors?|failed)\b' <<<"$summary"; then
    echo "RED $summary"
    return
  fi
  if ! grep -qE '[1-9][0-9]{2,} passed' <<<"$summary"; then
    echo "VACUOUS $summary"
    return
  fi
  echo "PASS $summary"
}

lint_out="$(mktemp)"
run "lint bucket" bash -o pipefail -c "uv run pytest -m lint -q -p no:cacheprovider 2>&1 | tee '$lint_out' | tail -3"
verdict="$(_lint_bucket_verdict "$lint_out")"
case "$verdict" in
  PASS*)
    rm -f "$lint_out"
    ;;
  RED*)
    echo "   RED: lint bucket -- ${verdict#RED }"
    echo "   (a stale gate jar errors every substrate test: scripts/build-gate-jar.sh)"
    echo "   full output kept at $lint_out"
    reds+=("lint bucket")
    ;;
  VACUOUS*)
    echo "   VACUOUS: lint bucket ran fewer than 100 tests -- ${verdict#VACUOUS }"
    echo "   full output kept at $lint_out"
    exit 2
    ;;
esac
run "pin tests" uv run pytest -q -p no:cacheprovider \
  tests/test_engine_version.py tests/test_plugin_release_drift_ledger.py \
  tests/test_ci_release_ledger_gate.py tests/test_pg_bundle_version_parity.py \
  tests/test_embed_deadline_default_ordering.py
run "wire-contract pairing" uv run python scripts/check_wire_contract_pairing.py
run "ruff (src, as CI)" uv run ruff check src
echo
if [ ${#reds[@]} -eq 0 ]; then echo "PINS PREFLIGHT PASSED"; exit 0; fi
printf 'PINS PREFLIGHT FAILED: %s\n' "${reds[*]}"; exit 1
