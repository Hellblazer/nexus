#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# One `claude plugin eval` run of a conexus tree inside the A/B image (nexus-dg7i6.7).
# Env: TREE (a conexus tree, read-only mount), RUNS, CASE_GLOB (optional), MAX_COST, CONC.
# Scaffolds on, the tools the outcome cases need granted, the real nexus MCP servers
# started (--mocks off) against the image's service, report kept local (--no-publish).
set -uo pipefail
OUT=/out
export PATH=/home/nexus/.local/bin:$PATH
rm -f /home/nexus/.config/nexus/postgres/postmaster.pid
nx daemon service start > "$OUT/service.log" 2>&1
PLUGIN=/home/nexus/pe/conexus
mkdir -p /home/nexus/pe && cp -R "${TREE:?TREE}" "$PLUGIN"
ARGS=(--runs "${RUNS:-3}" --ablation none --scaffold --trust-plugin --no-publish
      --mocks off --allow-tools Bash Write Edit mcp__plugin_conexus_nexus mcp__plugin_conexus_nexus-catalog mcp__plugin_conexus_sequential-thinking
      --model "${ANTHROPIC_MODEL:-claude-opus-5-5}" -j "${CONC:-2}"
      --max-cost-usd "${MAX_COST:-100}" --json "$OUT/report.json"
      --output-dir "$OUT/results" --report "$OUT/report.html")
[ -n "${CASE_GLOB:-}" ] && ARGS+=(--case "$CASE_GLOB")
[ -n "${KEEP_TEMP:-}" ] && ARGS+=(--keep-temp)
t0=$(date +%s)
CLAUDE_CODE_WALNUT_SPIRE=1 timeout 10800 claude plugin eval "${ARGS[@]}" "$PLUGIN" > "$OUT/eval.log" 2>&1
echo "eval exit=$? wall=$(( $(date +%s) - t0 ))" > "$OUT/exit.txt"
# With KEEP_TEMP the runs' traces and scaffold dirs survive the container.
[ -n "${KEEP_TEMP:-}" ] && cp -R /tmp/claude-eval-* "$OUT/" 2>/dev/null
chmod -R a+rwX "$OUT" 2>/dev/null
exit 0
