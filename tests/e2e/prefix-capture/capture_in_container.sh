#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Runs inside the prefix-capture container. /src = the nexus checkout (ro),
# /out = results (rw). PROMPT_FILE (default /out/prompt.txt) is the -p prompt.
set -uo pipefail
OUT=/out
PORT=${PROXY_PORT:-8899}
: > "$OUT/requests.jsonl"
python3 /home/nexus/proxy.py "$PORT" "$OUT/requests.jsonl" &
PROXY_PID=$!

# Plugins from the repo's own marketplace (sources are pinned release tags).
claude plugin marketplace add /src > "$OUT/plugin-install.log" 2>&1
claude plugin install conexus@nexus-plugins >> "$OUT/plugin-install.log" 2>&1
claude plugin install sn@nexus-plugins >> "$OUT/plugin-install.log" 2>&1
claude plugin list >> "$OUT/plugin-install.log" 2>&1

# A small project dir: the files the subagent tasks read, plus CLAUDE.md.
REPO=/home/nexus/repo
mkdir -p "$REPO"
cp /src/README.md /src/AGENTS.md /src/CHANGELOG.md "$REPO"/ 2>/dev/null
ln -s AGENTS.md "$REPO/CLAUDE.md"
git -C "$REPO" init -q && git -C "$REPO" config user.email cap@example.invalid \
  && git -C "$REPO" config user.name cap && git -C "$REPO" add -A \
  && git -C "$REPO" commit -qm init

cd "$REPO" || exit 1
ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" \
ENABLE_TOOL_SEARCH="${ENABLE_TOOL_SEARCH:-true}" \
  timeout 900 claude -p "$(cat "${PROMPT_FILE:-$OUT/prompt.txt}")" \
    --dangerously-skip-permissions --output-format json \
    --debug --debug-file "$OUT/debug.log" \
  > "$OUT/result.json" 2> "$OUT/claude.stderr"
echo "claude exit $?" >> "$OUT/plugin-install.log"
sleep 2
kill "$PROXY_PID" 2>/dev/null
# Session transcripts (conversation only) for cross-checking.
cp -R /home/nexus/.claude/projects "$OUT/projects" 2>/dev/null
python3 /home/nexus/analyse.py "$OUT/requests.jsonl" > "$OUT/analysis.txt" 2>&1
chmod -R a+rwX "$OUT" 2>/dev/null
exit 0
