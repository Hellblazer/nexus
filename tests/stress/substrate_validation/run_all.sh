#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run the full substrate-validation stress matrix (nexus-57pwo).
# Sequential, not parallel — per the project's no-parallel-tests rule.
# Total wallclock ~30 sec on a fast machine; longer if pip caches are cold.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$(git -C "$HERE" rev-parse --show-toplevel)"

mkdir -p "$HERE/receipts"
# Clean prior receipts so re-runs don't show stale state.
rm -f "$HERE/receipts/"*.json

echo "═══════════════════════════════════════════════════════════════════════"
echo "   RDR-120 substrate-validation stress matrix (nexus-57pwo)"
echo "   Replaces the §Approach Phase 6 30-day calendar soak."
echo "   5 scenarios, sequential, ~30-90s total wallclock."
echo "═══════════════════════════════════════════════════════════════════════"

for scenario in \
    scenario_1_fan_in \
    scenario_2_mixed_workload \
    scenario_3_kill9_recovery \
    scenario_4_schema_mismatch \
    scenario_5_catalog_rebuild
do
    echo ""
    echo "── running $scenario ─────────────────────────────────────────────"
    uv run python "$HERE/$scenario.py"
done

echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo "All 5 scenarios green."
echo "Receipts: $HERE/receipts/*.json"
echo ""
echo "Substrate is empirically validated. Per the moratorium-lift policy"
echo "(T2: 120-moratorium-lift-criterion-stress-not-calendar), consumer"
echo "RDRs may file regardless of calendar date."
echo "═══════════════════════════════════════════════════════════════════════"
