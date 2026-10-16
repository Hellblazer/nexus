#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Uncued sequential-thinking rate on the real nexus repo (nexus-4mptw).
# One container run. Env: ARM (A|C), GUIDE (G1|G2), PROMPT (file), ANTHROPIC_MODEL.
# Project = /harness/ab/seqthink/project.tar.gz (git archive of develop 005fd83ac);
# G2 adds the user's global CLAUDE.md as ~/.claude/CLAUDE.md.
set -uo pipefail
OUT=/out
SQ=/harness/ab/seqthink
TS=/harness/ab/tuplespace
PORT=8899
ARM=${ARM:?ARM}; GUIDE=${GUIDE:?GUIDE}
: > "$OUT/requests.jsonl"
python3 /harness/proxy.py "$PORT" "$OUT/requests.jsonl" &
PROXY_PID=$!
rm -f /home/nexus/.config/nexus/postgres/postmaster.pid
nx daemon service start > "$OUT/service.log" 2>&1

MCP=$(ls /home/nexus/.claude/plugins/cache/nexus-plugins/conexus/*/.mcp.json)
case "$ARM" in
  A) : ;;
  C)
    python3 - "$MCP" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["nexus"].pop("alwaysLoad", None)
json.dump(d, open(p, "w"), indent=2)
PY
    CORE_PY=/home/nexus/.local/share/uv/tools/conexus/lib/python3.12/site-packages/nexus/mcp/core.py
    cat "$TS/arm_c8_core_meta.py" >> "$CORE_PY" ;;
  *) echo "bad ARM $ARM" >&2; exit 2 ;;
esac
case "$GUIDE" in
  G1) rm -f /home/nexus/.claude/CLAUDE.md ;;
  G2) cp "$SQ/user_claude.md" /home/nexus/.claude/CLAUDE.md ;;
  *) echo "bad GUIDE $GUIDE" >&2; exit 2 ;;
esac
{ echo "arm=$ARM guide=$GUIDE"; wc -c /home/nexus/.claude/CLAUDE.md 2>&1; } > "$OUT/setup.txt"

REPO=/home/nexus/nexus
mkdir -p "$REPO" && tar -xzf "$SQ/project.tar.gz" -C "$REPO"
git -C "$REPO" init -q && git -C "$REPO" config user.email ab@example.invalid \
  && git -C "$REPO" config user.name ab && git -C "$REPO" add -A \
  && git -C "$REPO" commit -qm "snapshot of develop 005fd83ac"
{ wc -c "$REPO/AGENTS.md"; readlink "$REPO/CLAUDE.md"; } >> "$OUT/setup.txt"
cd "$REPO" || exit 1
SID=$(python3 -c 'import uuid;print(uuid.uuid4())')
echo "{\"S\":\"$SID\"}" > "$OUT/ids.json"
t0=$(date +%s)
ANTHROPIC_BASE_URL="http://127.0.0.1:${PORT}" ENABLE_TOOL_SEARCH=true \
  timeout 900 claude -p "$(cat "$PROMPT")" --session-id "$SID" \
    --dangerously-skip-permissions --output-format json \
    --debug --debug-file "$OUT/debug.S.log" \
  > "$OUT/result.S.json" 2> "$OUT/claude.S.stderr"
echo "S exit=$? wall=$(( $(date +%s) - t0 ))" >> "$OUT/exit.txt"
git -C "$REPO" status --short > "$OUT/repo_dirty.txt"
sleep 2
kill "$PROXY_PID" 2>/dev/null
gzip -f "$OUT"/debug.*.log 2>/dev/null
chmod -R a+rwX "$OUT" 2>/dev/null
exit 0
