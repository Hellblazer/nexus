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
# A path run reviews its edits in the MarkupEditor app, which a headless session cannot drive (nexus-scmcs.2.13):
# without PROSE_EDIT_APP_RUNNER=1 (set only by a harness that answers the MarkupEditor tools) such a scenario is
# recorded in OUT_DIR/not-run.txt with the reason, no session starts, and the exit is 3. verdicts.py scores it NOT-RUN.
# canary-fail and unmapped stop before the app by design and run (runner_guard.bash).
#
# PROSE_EDIT_APP_RUNNER=1 puts the stand-in MarkupEditor server (markup_standin.py, nexus-scmcs.3) in every session
# of the run: an --mcp-config file OUT_DIR/NAME.mcp.json naming it `markupeditor`, --strict-mcp-config (the machine's
# own MarkupEditor servers stay out), and the tools the skill calls allowed (never save_document or replace_blocks).
# The sessions of one run share OUT_DIR/markup-standin-state.json, so a resumed session finds what the last one
# proposed; NAME picks the author's scripted decisions (PROSE_EDIT_STANDIN_DECISIONS, default
# markup_standin_decisions.json). PROSE_EDIT_STANDIN_PYTHON names the interpreter that has the mcp package (default:
# the checkout's .venv, else python3). Without the switch nothing here changes.
#
# Tool restriction (the session is a scenario, not a person):
#   allowed   Bash only for the three prose-edit scripts, Read, Write, Agent
#   denied    Bash(nx:*), Bash(uv:*), Bash(bd:*), Bash(curl:*)
#   mode      dontAsk: anything not allowed is refused instead of prompting
# PROSE_EDIT_NX, PROSE_EDIT_PROJECT_PREFIX and TMPDIR pass
# through from the caller's environment.
set -u
NAME="${1:?name}"
PROMPT="${2:?prompt}"
OUT="${OUT_DIR:?set OUT_DIR}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(cd "$HERE/../../.." && pwd)"
export TMPDIR="${TMPDIR:-/tmp}"
. "$HERE/runner_guard.bash"
if [ "${PROSE_EDIT_APP_RUNNER:-0}" != 1 ] && runner_prompt_is_path_run "$PROMPT" && ! runner_stops_before_the_app "$NAME"; then
  runner_not_run "$OUT" "$NAME" "$RUNNER_NO_APP_REASON"
  exit 3
fi
mkdir -p "$OUT"
APP_ARGS=()
APP_TOOLS=()
if [ "${PROSE_EDIT_APP_RUNNER:-0}" = 1 ]; then
  OUT_ABS="$(cd "$OUT" && pwd)" || exit 1
  STANDIN_PY="${PROSE_EDIT_STANDIN_PYTHON:-}"
  if [ -z "$STANDIN_PY" ]; then
    if [ -x "$WT/.venv/bin/python" ]; then STANDIN_PY="$WT/.venv/bin/python"; else STANDIN_PY=python3; fi
  fi
  python3 "$HERE/markup_standin.py" mcp-config --out "$OUT_ABS/$NAME.mcp.json" --python "$STANDIN_PY" \
    --state "$OUT_ABS/markup-standin-state.json" \
    --decisions "${PROSE_EDIT_STANDIN_DECISIONS:-$HERE/markup_standin_decisions.json}" --scenario "$NAME" || exit 1
  APP_ARGS=(--mcp-config "$OUT_ABS/$NAME.mcp.json" --strict-mcp-config)
  for tool in open_document get_document propose_edits list_suggestions undo; do
    APP_TOOLS+=("mcp__markupeditor__$tool")
  done
fi
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
  --plugin-dir "$PWD/prose-edit" \
  --allowedTools "Bash(python3 $PWD/prose-edit/skills/prose-edit/scripts/brief.py:*)" \
                 "Bash(python3 $PWD/prose-edit/skills/prose-edit/scripts/memory.py:*)" \
                 "Bash(python3 $PWD/prose-edit/skills/prose-edit/scripts/review.py:*)" Read Write Agent \
                 ${APP_TOOLS[@]+"${APP_TOOLS[@]}"} ${APP_ARGS[@]+"${APP_ARGS[@]}"} \
  --disallowedTools "Bash(nx:*)" "Bash(uv:*)" "Bash(bd:*)" "Bash(curl:*)" \
  --add-dir "$TMPDIR" \
  --max-turns 40 > "$OUT/$NAME.jsonl" 2> "$OUT/$NAME.err"
echo "rc=$?" > "$OUT/$NAME.rc"
# Every runner goes through here, so every run leaves NAME.head (the checkout's HEAD sha) and NAME.sha256 (the
# transcript's hash) beside its transcript.
"$HERE/record-run.sh" "$OUT" "$NAME" || echo "record-run.sh failed for $NAME" >&2
