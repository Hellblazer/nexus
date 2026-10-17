#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-mcfaj: what does a PreToolUse hook `permissionDecision: "ask"` do when
# no human can answer the prompt? Real headless `claude -p` sessions, on chas
# only, in the nexus-cc-val container, under the harness's own automation
# token (RDR-219). Claude Code never runs on the laptop.
#
#   tests/cc-validation/hook-ask-headless/run.sh [run-id ...]
#   tests/cc-validation/hook-ask-headless/run.sh --summarize DIR
#
# WHY A SIBLING OF runner.sh AND NOT A NUMBERED SCENARIO. runner.sh scenarios
# drive an interactive TUI in a local tmux; this question is about `-p`, and
# the box rule is that Claude Code runs on chas. connection-race-ladder/ is
# the precedent for a cc-validation harness with its own launcher.
#
# THE MATRIX (batch.sh RUNS): default / dontAsk / auto, each as a control
# (hook fires, no decision) and an ask run; a background subagent inside a
# default-mode -p session; and one run through the shipped _lib.ask envelope.
# Verdicts come from summarize.py reading stream.jsonl + hook.log + rc, never
# from the model's own words. A row whose hook never fired is NOT-EXERCISED,
# not a pass. Billed sessions, haiku by default (MCFAJ_MODEL overrides).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=../../e2e/lib/python.sh disable=SC1091
source "$ROOT/tests/e2e/lib/python.sh"
e2e_python_resolve || exit 2
CRED_TOOL="$ROOT/tests/e2e/lib/claude_credentials.py"
REMOTE_HOST="${MCFAJ_HOST:-chas-test}"
REMOTE_DIR="${MCFAJ_REMOTE_DIR:-src/nexus-wt/mcfaj-hook-ask}"

if [ "${1:-}" = "--summarize" ]; then
    exec "$E2E_PYTHON" "$HERE/summarize.py" "${2:?evidence dir}"
fi

"$E2E_PYTHON" "$CRED_TOOL" status > /dev/null 2>&1 || {
    echo "HOOK-ASK-HEADLESS UNVERIFIED: no usable automation token" >&2
    exit 2
}

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/mcfaj-stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/scripts/routing"
cp -R "$HERE/hooks" "$HERE/batch.sh" "$HERE/in_container.sh" "$STAGE/"
# The real envelope under test, with the one module it imports.
cp "$ROOT/conexus/hooks/scripts/_endpoint_resolve.py" "$STAGE/scripts/"
cp "$ROOT/conexus/hooks/scripts/routing/_lib.py" "$STAGE/scripts/routing/"

echo "[stage] -> $REMOTE_HOST:$REMOTE_DIR"
# tar over ssh: chas has no rsync. Keep the remote out/ (prior evidence).
ssh "$REMOTE_HOST" "mkdir -p '$REMOTE_DIR' && cd '$REMOTE_DIR' && rm -rf hooks scripts batch.sh in_container.sh"
COPYFILE_DISABLE=1 tar --no-xattrs -C "$STAGE" -cf - . | ssh "$REMOTE_HOST" "tar -C '$REMOTE_DIR' -xf - && chmod a+rx '$REMOTE_DIR' && chmod -R a+rX '$REMOTE_DIR/hooks' '$REMOTE_DIR/scripts' '$REMOTE_DIR/batch.sh' '$REMOTE_DIR/in_container.sh'"

# lab-run is the shared pool's wrapper; the script is a file because ssh
# re-splits a string. The token reaches it through this process's
# environment (the helper's reader), never through argv.
REMOTE_ABS="$(ssh "$REMOTE_HOST" "cd '$REMOTE_DIR' && pwd")"
"$E2E_PYTHON" "$CRED_TOOL" run --remote "$REMOTE_HOST" -- \
    lab-run -c lab-s -- /bin/bash "$REMOTE_ABS/batch.sh" "$@"

LOCAL_OUT="${MCFAJ_OUT:-$HERE/out}"
mkdir -p "$LOCAL_OUT"
ssh "$REMOTE_HOST" "tar -C '$REMOTE_DIR/out' -cf - ." | tar -C "$LOCAL_OUT" -xf -
"$E2E_PYTHON" "$HERE/summarize.py" "$LOCAL_OUT"
