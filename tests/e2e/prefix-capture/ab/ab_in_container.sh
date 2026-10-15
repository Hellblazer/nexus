#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-ly9c2: one A/B run inside the nexus-ab-seeded image (seeded local
# engine, conexus+sn plugins already installed). Env: ARM (A|B|C), TASK_FILE
# (prompt), ANTHROPIC_MODEL, ENABLE_TOOL_SEARCH, CLAUDE_CODE_OAUTH_TOKEN.
# /harness = tests/e2e/prefix-capture (ro), /out = this run's results (rw).
set -uo pipefail
OUT=/out
PORT=${PROXY_PORT:-8899}
ARM=${ARM:?ARM}
: > "$OUT/requests.jsonl"
python3 /harness/proxy.py "$PORT" "$OUT/requests.jsonl" &
PROXY_PID=$!

# Backend: the image holds a cleanly stopped cluster seeded by seed.sh.
rm -f /home/nexus/.config/nexus/postgres/postmaster.pid
nx daemon service start > "$OUT/service.log" 2>&1

# Arm patch on the installed conexus plugin's .mcp.json (nexus server only).
MCP=$(ls /home/nexus/.claude/plugins/cache/nexus-plugins/conexus/*/.mcp.json)
# C leaves the server key UNSET: in 2.1.300 a server-level alwaysLoad false
# sets serverDefersAllTools, which overrides any per-tool _meta flag; unset,
# MCP tools default to deferred and the per-tool flag decides.
case "$ARM" in
  A) LOAD=true ;;
  B) LOAD=false ;;
  C) LOAD=unset ;;
  *) echo "bad ARM $ARM" >&2; exit 2 ;;
esac
python3 - "$MCP" "$LOAD" <<'PY'
import json, sys
p, load = sys.argv[1], sys.argv[2]
d = json.load(open(p))
if load == "unset":
    d["nexus"].pop("alwaysLoad", None)
else:
    d["nexus"]["alwaysLoad"] = load == "true"
json.dump(d, open(p, "w"), indent=2)
PY
if [ "$ARM" = C ]; then
  # Experiment-only: mark the core tools eager per tool. Claude Code 2.1.300
  # treats an MCP tool as always-loaded when its tools/list entry carries
  # _meta["anthropic/alwaysLoad"] = true, whatever the server's alwaysLoad.
  CORE_PY=/home/nexus/.local/share/uv/tools/conexus/lib/python3.12/site-packages/nexus/mcp/core.py
  cat /harness/ab/arm_c_core_meta.py >> "$CORE_PY"
fi
jq -c '.nexus | {alwaysLoad}' "$MCP" > "$OUT/arm.json"

# The project: small, deterministic, the same in every run.
REPO=/home/nexus/repo
mkdir -p "$REPO/src"
cp -R /harness/ab/project/. "$REPO"/
git -C "$REPO" init -q && git -C "$REPO" config user.email ab@example.invalid \
  && git -C "$REPO" config user.name ab && git -C "$REPO" add -A \
  && git -C "$REPO" commit -qm "Add ingest batching with an injected clock"

cd "$REPO" || exit 1
T0=$(date +%s.%N)
ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" \
ENABLE_TOOL_SEARCH="${ENABLE_TOOL_SEARCH:-true}" \
  timeout 600 claude -p "$(cat "$TASK_FILE")" \
    --dangerously-skip-permissions --output-format json \
    --debug --debug-file "$OUT/debug.log" \
  > "$OUT/result.json" 2> "$OUT/claude.stderr"
echo "exit=$? wall=$(python3 -c "import time;print(round(time.time()-$T0,2))")" > "$OUT/exit.txt"
sleep 2
kill "$PROXY_PID" 2>/dev/null
# Post-state for the write tasks.
{
  echo "## memory aurora"; nx memory list -p aurora 2>&1
  echo "## search checkpoint"; nx search "checkpoint every 30 seconds recovery" --corpus knowledge 2>&1 | grep '^\['
} > "$OUT/poststate.txt"
cp -R /home/nexus/.claude/projects "$OUT/projects" 2>/dev/null
gzip -f "$OUT/debug.log" 2>/dev/null
chmod -R a+rwX "$OUT" 2>/dev/null
exit 0
