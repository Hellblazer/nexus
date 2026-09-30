#!/bin/bash
# usage: OUT_DIR=<dir> run-scenario.sh <name> <prompt>
#
# Runs one headless Claude Code session in this checkout and saves NAME.jsonl (stream-json),
# NAME.err and NAME.rc under OUT_DIR. The automation token reaches the child through
# tests/e2e/lib/claude_credentials.py and is never printed.
#
# Tool restriction (the session is a scenario, not a person):
#   allowed   Bash only for the two prose-edit scripts, Read, Write, Agent
#   denied    Bash(nx:*), Bash(uv:*), Bash(bd:*), Bash(curl:*)
#   mode      dontAsk: anything not allowed is refused instead of prompting
# PROSE_EDIT_NX, PROSE_EDIT_PROJECT_PREFIX and TMPDIR pass through from the caller's environment.
set -u
NAME="${1:?name}"
PROMPT="${2:?prompt}"
OUT="${OUT_DIR:?set OUT_DIR}"
WT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export TMPDIR="${TMPDIR:-/tmp}"
mkdir -p "$OUT"
cd "$WT" || exit 1
python3 tests/e2e/lib/claude_credentials.py run -- "${CLAUDE_BIN:-claude}" -p "$PROMPT" \
  --output-format stream-json --verbose \
  --permission-mode dontAsk \
  --allowedTools "Bash(python3 .claude/skills/prose-edit/scripts/brief.py:*)" \
                 "Bash(python3 .claude/skills/prose-edit/scripts/memory.py:*)" Read Write Agent \
  --disallowedTools "Bash(nx:*)" "Bash(uv:*)" "Bash(bd:*)" "Bash(curl:*)" \
  --add-dir "$TMPDIR" \
  --max-turns 40 > "$OUT/$NAME.jsonl" 2> "$OUT/$NAME.err"
echo "rc=$?" > "$OUT/$NAME.rc"
