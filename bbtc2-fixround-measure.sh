#!/usr/bin/env bash
# run from the worktree root; outputs under bbtc2-out-fr-m/
set -u
O=bbtc2-out-fr-m; rm -rf $O; mkdir -p $O
scripts/build-gate-jar.sh > $O/jar.log 2>&1; echo jar_exit=$? | tee -a $O/results.txt
for W in 2 0; do
  NX_SEARCH_ROUTE_PROBE_WORKERS=$W PYTHONPATH=. uv run python scripts/bbtc2_query_e.py --collections 120 --scale 6 \
    --skip-explain --out $O/w$W.json > $O/w$W.log 2>&1
  echo "harness_w$W exit=$?" | tee -a $O/results.txt
  grep -A4 "== medians" $O/w$W.log | tee -a $O/results.txt
done
