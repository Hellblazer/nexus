#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#   pe_mac.sh <label> [RUNS] [CASE_GLOB]   run claude plugin eval on a macOS host, detached;
#   results in $EVAL_ROOT/out/pe-<label>/ (nexus-fi1qg). Launch through
#   tests/e2e/lib/claude_credentials.py run --remote hellmini -- bash <this> ...
# macOS, not docker: Claude Code's Bash sandbox there is Seatbelt, not bubblewrap, so it
# does not need the nested user namespace that chas's AppArmor userns restriction refuses
# (conexus/evals/README.md). EVAL_ROOT holds conexus/, a develop tree exported with
# NX_NO_TELEMETRY=1 added to every server in its .mcp.json.
set -uo pipefail
EVAL_ROOT=${EVAL_ROOT:-/Volumes/Bulk/src/evals-hellmini}
LABEL=${1:?label}; RUNS=${2:-3}; GLOB=${3:-}
OUT="$EVAL_ROOT/out/pe-$LABEL"; mkdir -p "$OUT"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" NX_NO_TELEMETRY=1
# Known limit: inside the sandbox a bare `git` resolves to the /usr/bin/git shim whatever
# PATH says, and the shim fails there ("Failed to locate 'git'"); DEVELOPER_DIR does not
# reach the sandbox. Bash itself works. See conexus/evals/README.md.
# PLUGIN_DIR overrides the tree under test (e.g. a one-case probe corpus).
ARGS=(--runs "$RUNS" --ablation none --scaffold --trust-plugin --no-publish
      --mocks off --allow-tools Bash Write Edit mcp__plugin_conexus_nexus mcp__plugin_conexus_nexus-catalog mcp__plugin_conexus_sequential-thinking
      --model "${AB_MODEL:-claude-opus-5-5}" -j "${CONC:-2}"
      --max-cost-usd "${MAX_COST:-100}" --json "$OUT/report.json"
      --output-dir "$OUT/results" --report "$OUT/report.html")
[ -n "$GLOB" ] && ARGS+=(--case "$GLOB")
[ -n "${KEEP_TEMP:-}" ] && ARGS+=(--keep-temp)
nohup bash -c '
  t0=$(date +%s)
  CLAUDE_CODE_WALNUT_SPIRE=1 claude plugin eval "$@" "'"${PLUGIN_DIR:-$EVAL_ROOT/conexus}"'" > "'"$OUT"'/eval.log" 2>&1
  echo "eval exit=$? wall=$(( $(date +%s) - t0 ))" > "'"$OUT"'/exit.txt"
' _ "${ARGS[@]}" > "$OUT/nohup.log" 2>&1 < /dev/null &
disown
echo "detached pe-$LABEL"
