#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Arm C validation (tuplespace, SubagentStart hook, sequential thinking).
# One container run. Env: ARM (A|C), MODE (single|tuple), PROMPT (single mode:
# prompt file under /harness), ANTHROPIC_MODEL, CLAUDE_CODE_OAUTH_TOKEN.
# tuple mode runs three claude -p sessions against one engine:
#   S1 subscribes a name and mailbox_sends to agent id A2 and session id U3;
#   S2 tuple_in + tuple_ack on mailbox/A2; S3 (session U3) gets an uncued
#   decision prompt, so the UserPromptSubmit drain should render U3's mail.
set -uo pipefail
OUT=/out
TS=/harness/ab/tuplespace
PORT=8899
ARM=${ARM:?ARM}; MODE=${MODE:?MODE}
: > "$OUT/requests.jsonl"
python3 /harness/proxy.py "$PORT" "$OUT/requests.jsonl" &
PROXY_PID=$!

rm -f /home/nexus/.config/nexus/postgres/postmaster.pid
nx daemon service start > "$OUT/service.log" 2>&1

MCP=$(ls /home/nexus/.claude/plugins/cache/nexus-plugins/conexus/*/.mcp.json)
case "$ARM" in
  A) : ;;  # installed 7.79.0 .mcp.json unchanged (nexus alwaysLoad true)
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
python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(json.dumps({k:v.get('alwaysLoad','UNSET') for k,v in d.items()}))" "$MCP" > "$OUT/arm.json"

REPO=/home/nexus/repo
mkdir -p "$REPO/src"
cp -R /harness/ab/project/. "$REPO"/
git -C "$REPO" init -q && git -C "$REPO" config user.email ab@example.invalid \
  && git -C "$REPO" config user.name ab && git -C "$REPO" add -A \
  && git -C "$REPO" commit -qm "Add ingest batching with an injected clock"
cd "$REPO" || exit 1

run_claude() {  # <label> <session-uuid> <prompt-text>
  local label=$1 sid=$2 prompt=$3 t0
  t0=$(date +%s.%N)
  ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" ENABLE_TOOL_SEARCH=true \
    timeout 600 claude -p "$prompt" --session-id "$sid" \
      --dangerously-skip-permissions --output-format json \
      --debug --debug-file "$OUT/debug.$label.log" \
    > "$OUT/result.$label.json" 2> "$OUT/claude.$label.stderr"
  echo "$label exit=$? sid=$sid wall=$(python3 -c "import time;print(round(time.time()-$t0,2))")" >> "$OUT/exit.txt"
}
stats() {  # <label>
  { echo "## $1"
    for s in "${SUBS[@]}"; do echo "# $s"; nx tuple stats "$s" 2>&1; done
  } >> "$OUT/poststate.txt"
}

if [ "$MODE" = single ]; then
  SID=$(python3 -c 'import uuid;print(uuid.uuid4())')
  echo "{\"S\":\"$SID\"}" > "$OUT/ids.json"
  run_claude S "$SID" "$(cat "$PROMPT")"
else
  NONCE=$(python3 -c 'import secrets;print(secrets.token_hex(4))')
  A2=a$(python3 -c 'import secrets;print(secrets.token_hex(8))')
  U1=$(python3 -c 'import uuid;print(uuid.uuid4())')
  U2=$(python3 -c 'import uuid;print(uuid.uuid4())')
  U3=$(python3 -c 'import uuid;print(uuid.uuid4())')
  NAME="ab-s1-$NONCE"
  printf '{"NONCE":"%s","A2":"%s","U1":"%s","U2":"%s","U3":"%s","NAME":"%s"}\n' \
    "$NONCE" "$A2" "$U1" "$U2" "$U3" "$NAME" > "$OUT/ids.json"
  SUBS=("mailbox/$A2" "mailbox/$U3")
  sub() { sed -e "s/{NONCE}/$NONCE/g" -e "s/{A2}/$A2/g" -e "s/{U3}/$U3/g" -e "s/{NAME}/$NAME/g" "$1"; }
  stats before
  run_claude S1 "$U1" "$(sub "$TS/prompts/s1_send.txt")"
  stats after_S1
  { echo "## directory $NAME"; nx tuple directory "$NAME" 2>&1; } >> "$OUT/poststate.txt"
  run_claude S2 "$U2" "$(sub "$TS/prompts/s2_claim.txt")"
  stats after_S2
  run_claude S3 "$U3" "$(sub "$TS/prompts/s3_uncued.txt")"
  stats after_S3
fi
sleep 2
kill "$PROXY_PID" 2>/dev/null
gzip -f "$OUT"/debug.*.log 2>/dev/null
chmod -R a+rwX "$OUT" 2>/dev/null
exit 0
