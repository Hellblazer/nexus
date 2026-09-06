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
lint_out="$(mktemp)"
run "lint bucket" bash -o pipefail -c "uv run pytest -m lint -q -p no:cacheprovider 2>&1 | tee '$lint_out' | tail -3"
if grep -qE '[0-9]+ (errors?|failed)( |,)' "$lint_out"; then
  echo "   RED: lint bucket reported failures or setup errors (a stale gate jar errors every substrate test: scripts/build-gate-jar.sh)"; reds+=("lint bucket")
elif ! grep -qE '[1-9][0-9]{2,} passed' "$lint_out"; then
  echo "   VACUOUS: lint bucket ran fewer than 100 tests"; rm -f "$lint_out"; exit 2
fi
rm -f "$lint_out"
run "pin tests" uv run pytest -q -p no:cacheprovider \
  tests/test_engine_version.py tests/test_plugin_release_drift_ledger.py \
  tests/test_ci_release_ledger_gate.py tests/test_pg_bundle_version_parity.py \
  tests/test_embed_deadline_default_ordering.py
run "wire-contract pairing" uv run python scripts/check_wire_contract_pairing.py
run "ruff (src, as CI)" uv run ruff check src
echo
if [ ${#reds[@]} -eq 0 ]; then echo "PINS PREFLIGHT PASSED"; exit 0; fi
printf 'PINS PREFLIGHT FAILED: %s\n' "${reds[*]}"; exit 1
