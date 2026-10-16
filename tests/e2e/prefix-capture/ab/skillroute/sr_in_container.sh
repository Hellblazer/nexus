#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Skill routing under distractor plugins (nexus-dg7i6.1). One container run.
# Env: ARM (S0|S1|S2), CASE (a conexus/evals case directory name), ANTHROPIC_MODEL.
#   S0  the image's installed conexus + sn, nexus tools as shipped (8 eager)
#   S1  S0 + every plugin under /plugins/S1 (the beads plugin)
#   S2  S0 + every plugin under /plugins/S2 (the rest of the laptop's enabled set)
# /plugins is ab/skillroute/plugins (not committed; built by snapshot_plugins.sh).
set -uo pipefail
OUT=/out
TS=/harness/ab/tuplespace
PORT=8899
ARM=${ARM:?ARM}; CASE=${CASE:?CASE}
: > "$OUT/requests.jsonl"
python3 /harness/proxy.py "$PORT" "$OUT/requests.jsonl" &
PROXY_PID=$!
rm -f /home/nexus/.config/nexus/postgres/postmaster.pid
nx daemon service start > "$OUT/service.log" 2>&1

# Ship-shape nexus tools (arm C of nexus-ly9c2, as on develop since 7.80.0).
MCP=$(ls /home/nexus/.claude/plugins/cache/nexus-plugins/conexus/*/.mcp.json)
python3 - "$MCP" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["nexus"].pop("alwaysLoad", None)
json.dump(d, open(p, "w"), indent=2)
PY
cat "$TS/arm_c8_core_meta.py" >> /home/nexus/.local/share/uv/tools/conexus/lib/python3.12/site-packages/nexus/mcp/core.py

PLUGIN_ARGS=()
case "$ARM" in
  S0) : ;;
  S1|S2) for d in /plugins/"$ARM"/*/; do PLUGIN_ARGS+=(--plugin-dir "${d%/}"); done ;;
  *) echo "bad ARM $ARM" >&2; exit 2 ;;
esac
printf '%s\n' "arm=$ARM case=$CASE" "${PLUGIN_ARGS[@]}" > "$OUT/setup.txt"

# The prompt is the case's prompt.md body, frontmatter stripped.
PROMPT=$(awk 'f>=2{print} /^---$/{f++}' "/src/conexus/evals/$CASE/prompt.md")

REPO=/home/nexus/repo
mkdir -p "$REPO"
cp -R /harness/ab/project/. "$REPO"/
git -C "$REPO" init -q && git -C "$REPO" config user.email ab@example.invalid \
  && git -C "$REPO" config user.name ab && git -C "$REPO" add -A \
  && git -C "$REPO" commit -qm "Add ingest batching with an injected clock"
cd "$REPO" || exit 1
SID=$(python3 -c 'import uuid;print(uuid.uuid4())')
echo "{\"S\":\"$SID\"}" > "$OUT/ids.json"
t0=$(date +%s)
ANTHROPIC_BASE_URL="http://127.0.0.1:${PORT}" ENABLE_TOOL_SEARCH=true \
  timeout 600 claude -p "$PROMPT" --session-id "$SID" "${PLUGIN_ARGS[@]}" \
    --dangerously-skip-permissions --output-format json \
    --debug --debug-file "$OUT/debug.S.log" \
  > "$OUT/result.S.json" 2> "$OUT/claude.S.stderr"
echo "S exit=$? wall=$(( $(date +%s) - t0 ))" >> "$OUT/exit.txt"
sleep 2
kill "$PROXY_PID" 2>/dev/null
cp -R /home/nexus/.claude/projects "$OUT/projects" 2>/dev/null
gzip -f "$OUT"/debug.*.log 2>/dev/null
chmod -R a+rwX "$OUT" 2>/dev/null
exit 0
