#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run cc-validation scenarios in a container (nexus-ueztp). Claude Code runs
# only in a container on a Linux test host (chas), never on a developer laptop.
#
#   python3 tests/e2e/lib/claude_credentials.py run --remote chas-test -- \
#       <checkout>/tests/cc-validation/container/run.sh [--scenario 32]
#
# `claude_credentials.py run` puts the harness's automation token in this
# script's environment; the `docker run` below passes it into the container as
# a bare `-e CLAUDE_CODE_OAUTH_TOKEN`, so it never touches argv or a file.
# Nothing here prints it.
#
# What it does: stages the tracked harness files (tests/cc-validation, the
# e2e helpers, and the conexus plugin of THIS checkout), extracts the control
# plugin from the git tag v7.64.1 with `git archive` (fetching the tag when the
# clone lacks it), builds the toolchain image, and runs the scenario inside it
# with the stage mounted read-only and an evidence directory mounted
# read-write. Sessions are real and billed. Exits with the scenario's status.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
SCENARIO="32"
CONTROL_TAG="v7.64.1"
while [ $# -gt 0 ]; do
    case "$1" in
        --scenario) SCENARIO="${2:?--scenario needs a number}"; shift 2 ;;
        # ssh does not forward the caller's environment, so a scenario knob
        # travels as an argument (scenario 32's permission mode: auto|default).
        --s32-mode) export CC_VAL_S32_MODE="${2:?--s32-mode needs auto|default}"; shift 2 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
command -v docker > /dev/null || { echo "docker is required" >&2; exit 2; }
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "CC-VAL CONTAINER UNVERIFIED: no automation token in the environment;" >&2
    echo "run this under tests/e2e/lib/claude_credentials.py run [--remote HOST] --" >&2
    exit 2
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/cc-val-stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
EVID="${NX_CC_VAL_EVIDENCE:-${TMPDIR:-/tmp}/cc-val-evidence-$STAMP}"
mkdir -p "$EVID"; chmod 777 "$EVID"

echo "[stage] harness + current plugin from $(git -C "$ROOT" rev-parse --short HEAD) (tracked and new files, working-tree content)"
mkdir -p "$STAGE/harness" "$STAGE/control"
( cd "$ROOT" && git ls-files -z -co --exclude-standard -- tests/cc-validation tests/e2e/lib.sh tests/e2e/lib conexus ) \
    | tar -C "$ROOT" --null -T - -cf - | tar -x -C "$STAGE/harness"

if ! git -C "$ROOT" rev-parse -q --verify "refs/tags/$CONTROL_TAG" > /dev/null 2>&1; then
    git -C "$ROOT" fetch -q origin "refs/tags/$CONTROL_TAG:refs/tags/$CONTROL_TAG"
fi
echo "[stage] control plugin from tag $CONTROL_TAG ($(git -C "$ROOT" rev-parse --short "$CONTROL_TAG^{commit}"))"
git -C "$ROOT" archive "$CONTROL_TAG" conexus | tar -x -C "$STAGE/control"
chmod -R a+rX "$STAGE"

IMAGE="nexus-cc-val:local"
echo "[build] $IMAGE"
docker build -q -t "$IMAGE" "$HERE" > "$STAGE/docker-build.log" 2>&1 \
    || { tail -40 "$STAGE/docker-build.log" >&2; exit 1; }

echo "[run] scenario $SCENARIO; evidence -> $EVID"
set +e
# Bare -e CLAUDE_CODE_OAUTH_TOKEN (no value): docker copies it from its own
# environment. ANTHROPIC_MODEL / CLAUDE_CODE_SUBAGENT_MODEL are the harness's
# low-cost defaults claude_credentials.py run sets (haiku) and are passed the
# same way. NX_NO_TELEMETRY: a virgin HOME registers as a new install otherwise.
docker run --rm \
    -e CLAUDE_CODE_OAUTH_TOKEN \
    -e ANTHROPIC_MODEL \
    -e CLAUDE_CODE_SUBAGENT_MODEL \
    -e NX_NO_TELEMETRY=1 \
    -e CC_VAL_S32_MODE \
    -v "$STAGE/harness:/stage:ro" \
    -v "$STAGE/control:/control:ro" \
    -v "$EVID:/evidence" \
    --entrypoint /bin/bash \
    "$IMAGE" /stage/tests/cc-validation/container/entrypoint_in_container.sh "$SCENARIO"
rc=$?
set -e
echo "[run] exit $rc; evidence:"
find "$EVID" -type f 2>/dev/null | awk 'NR<=40' | while read -r f; do
    printf '  %-70s %s bytes\n' "${f#$EVID/}" "$(wc -c < "$f")"
done
exit $rc
