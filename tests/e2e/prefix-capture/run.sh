#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run from the LAPTOP's nexus checkout (it owns the keychain automation token).
# Claude Code itself runs only on chas, in a container.
#   tests/e2e/prefix-capture/run.sh <run-name> [prompt-file]
# Env: WT (chas worktree path), CC_VERSION, CONEXUS_VERSION, ANTHROPIC_MODEL,
#      ENABLE_TOOL_SEARCH, EXTRA_DOCKER_ARGS (e.g. "-e CLAUDE_CODE_SUBAGENT_MODEL=...").
# Results land in $WT/tests/e2e/prefix-capture/out/<run-name>/ on chas.
set -euo pipefail
HOST=${HOST:-chas-test}
WT=${WT:-/home/nxtest/src/nexus-wt/prefix-capture-4mptw}
NAME=${1:?run name}
PROMPT=${2:-}
HERE_REMOTE=$WT/tests/e2e/prefix-capture
OUT=$HERE_REMOTE/out/$NAME
IMAGE=nexus-prefix-capture:${CC_VERSION:-2.1.300}
ROOT=$(git rev-parse --show-toplevel)
# shellcheck source=../lib/python.sh disable=SC1091
source "$ROOT/tests/e2e/lib/python.sh"
e2e_python_resolve || exit 2

ssh "$HOST" "docker build -q -t $IMAGE --build-arg CC_VERSION=${CC_VERSION:-2.1.300} \
  --build-arg CONEXUS_VERSION=${CONEXUS_VERSION:-7.79.0} $HERE_REMOTE && \
  mkdir -p $OUT && chmod 777 $OUT" </dev/null
if [ -n "$PROMPT" ]; then
  scp -q "$PROMPT" "$HOST:$OUT/prompt.txt"
else
  ssh "$HOST" "cp $HERE_REMOTE/prompt.txt $OUT/prompt.txt" </dev/null
fi
# shellcheck disable=SC2086
"$E2E_PYTHON" "$ROOT/tests/e2e/lib/claude_credentials.py" run --remote "$HOST" -- \
  docker run --rm --name "prefix-capture-$NAME" -e CLAUDE_CODE_OAUTH_TOKEN -e NX_NO_TELEMETRY=1 \
    -e ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-opus-5-5}" \
    -e ENABLE_TOOL_SEARCH="${ENABLE_TOOL_SEARCH:-true}" \
    ${EXTRA_DOCKER_ARGS:-} \
    -v "$WT:/src:ro" -v "$OUT:/out" "$IMAGE"
ssh "$HOST" "cat $OUT/analysis.txt" </dev/null
