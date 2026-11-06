#!/usr/bin/env bash
# RDR-243 Phase 2, nexus-rfvyy.7.16 part 2: one bounded live measurement of sensitivity=exclude on a lab host.
# Run from the worktree root, as a background command with output redirected (lab-remote holds ssh until exit):
#   scripts/spikes/rdr243_plan/run.sh > /tmp/rdr243-plan.log 2>&1
# The harness is service/src/test/java/dev/nexus/service/vectors/VectorSensitivityExcludePlanMeasure.java; it does
# nothing unless NX_RDR243_PLAN_MEASURE is set. The engine build goes through scripts/mvnw-leased.sh with NO --lab-run:
# lab-remote already holds the grant, and --lab-run inside a grant exits 64.
# Sizes (env, defaults): NX_RDR243_PLAN_ROWS_A=80000 ROWS_B=30000 ROWS_C=18000 QUERIES=100 BUDGET_MIN=24.
# Output: service/target/rdr243-plan/measure.csv and measure.plans.txt, fetched back to this worktree.
set -euo pipefail
exec lab-remote -c lab-l --wait 30 --fetch service/target/rdr243-plan -- \
  env NX_RDR243_PLAN_MEASURE=1 NX_RDR243_PLAN_OUT=target/rdr243-plan/measure \
  scripts/mvnw-leased.sh -q test -Dtest=VectorSensitivityExcludePlanMeasure
