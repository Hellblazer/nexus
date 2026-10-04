#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# usage: OUT_DIR=<dir> [RESUME_FROM=<earlier name>] run-scenario.sh <name> <prompt>
#
# RESUME_FROM continues the session of an earlier run in OUT_DIR (claude --resume), which is how a
# scenario gives the author's answer to a skill that ended its turn with a question.
#
# Runs one headless Claude Code session in this checkout and saves NAME.jsonl (stream-json),
# NAME.err and NAME.rc under OUT_DIR, then NAME.head and NAME.sha256 (record-run.sh). The automation token reaches the child through
# tests/e2e/lib/claude_credentials.py and is never printed.
#
# Tool restriction (the session is a scenario, not a person):
#   allowed   Bash only for the three prose-edit scripts, Read, Write, Agent
#   denied    Bash(nx:*), Bash(uv:*), Bash(bd:*), Bash(curl:*)
#   mode      dontAsk: anything not allowed is refused instead of prompting
# PROSE_EDIT_NX, PROSE_EDIT_OPEN (honoured only with PROSE_EDIT_TEST=1), PROSE_EDIT_PROJECT_PREFIX and TMPDIR pass
# through from the caller's environment.
set -u
NAME="${1:?name}"
PROMPT="${2:?prompt}"
OUT="${OUT_DIR:?set OUT_DIR}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(cd "$HERE/../../.." && pwd)"
export TMPDIR="${TMPDIR:-/tmp}"
mkdir -p "$OUT"
cd "$WT" || exit 1
RESUME_ARGS=()
if [ -n "${RESUME_FROM:-}" ]; then
  SID="$(python3 -c 'import json,sys
for line in open(sys.argv[1]):
    sid = json.loads(line).get("session_id")
    if sid:
        print(sid)
        break' "$OUT/$RESUME_FROM.jsonl")"
  [ -n "$SID" ] || { echo "no session id in $OUT/$RESUME_FROM.jsonl" >&2; exit 1; }
  RESUME_ARGS=(--resume "$SID")
fi
python3 tests/e2e/lib/claude_credentials.py run -- "${CLAUDE_BIN:-claude}" -p "$PROMPT" ${RESUME_ARGS[@]+"${RESUME_ARGS[@]}"} \
  --output-format stream-json --verbose \
  --permission-mode dontAsk \
  --allowedTools "Bash(python3 .claude/skills/prose-edit/scripts/brief.py:*)" \
                 "Bash(python3 .claude/skills/prose-edit/scripts/memory.py:*)" \
                 "Bash(python3 .claude/skills/prose-edit/scripts/review.py:*)" Read Write Agent \
  --disallowedTools "Bash(nx:*)" "Bash(uv:*)" "Bash(bd:*)" "Bash(curl:*)" \
  --add-dir "$TMPDIR" \
  --max-turns 40 > "$OUT/$NAME.jsonl" 2> "$OUT/$NAME.err"
echo "rc=$?" > "$OUT/$NAME.rc"
# Every runner goes through here, so every run leaves NAME.head (the checkout's HEAD sha) and NAME.sha256 (the
# transcript's hash) beside its transcript.
"$HERE/record-run.sh" "$OUT" "$NAME" || echo "record-run.sh failed for $NAME" >&2
