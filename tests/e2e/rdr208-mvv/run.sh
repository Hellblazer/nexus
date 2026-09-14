#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-208 local-mode MVV (bead nexus-galkv.19): session-id mail addressing on
# a virgin LOCAL-mode box, in a container, through the real hooks, watcher and
# mailbox_send against the bundled engine. See mvv_in_container.sh.
#
#   tests/e2e/rdr208-mvv/run.sh                     # wheel and hooks from this checkout
#   tests/e2e/rdr208-mvv/run.sh --published 7.46.0  # the published wheel, that tag's hooks
#
# Step 6 (/branch) follows the drain hook under test: one carrying the
# nexus-galkv.19 fix must stop the parent's watcher; one without it must
# reproduce the 7.46.0 defect. Ends "RDR-208 LOCAL-MODE MVV PASSED" or FAILED.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
PUBLISHED=""
while [ $# -gt 0 ]; do
    case "$1" in
        --published) PUBLISHED="${2:?--published needs a version}"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
command -v docker > /dev/null || { echo "docker is required" >&2; exit 2; }

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/rdr208-mvv.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
cp "$HERE/Dockerfile" "$HERE/mvv_in_container.sh" "$HERE/session_server.sh" "$HERE/send.py" "$STAGE/"
mkdir -p "$STAGE/wheel" "$STAGE/hooks"
if [ -n "$PUBLISHED" ]; then
    LABEL="published-$PUBLISHED"
    git -C "$ROOT" rev-parse --verify -q "v$PUBLISHED^{commit}" > /dev/null \
        || { echo "no tag v$PUBLISHED in $ROOT" >&2; exit 2; }
    git -C "$ROOT" archive "v$PUBLISHED" conexus/hooks/scripts | tar -x -C "$STAGE"
    cp -R "$STAGE/conexus/hooks/scripts/." "$STAGE/hooks/"
    rm -rf "$STAGE/conexus"
    printf 'conexus==%s\n' "$PUBLISHED" > "$STAGE/wheel/SPEC"
else
    DIRTY=""
    [ -z "$(git -C "$ROOT" status --porcelain)" ] || DIRTY="-dirty"
    LABEL="tree-$(git -C "$ROOT" rev-parse --short HEAD)$DIRTY"
    uv build --wheel --out-dir "$STAGE/wheel" "$ROOT" > "$STAGE/build.log" 2>&1 \
        || { cat "$STAGE/build.log" >&2; exit 1; }
    cp -R "$ROOT/conexus/hooks/scripts/." "$STAGE/hooks/"
fi
# The expectation follows the hook, never a flag, so a published release that
# carries the fix is held to it (tests/test_rdr208_mvv_wiring.py pins the name).
if grep -q '_session_marker_names' "$STAGE/hooks/mailbox_drain.py"; then EXPECT=1; else EXPECT=0; fi

IMAGE="nexus-rdr208-mvv:$(printf '%s' "$LABEL" | tr -c 'a-z0-9_.-' '-')"
echo "building $IMAGE (expect_branch_fix=$EXPECT)"
docker build -q -t "$IMAGE" "$STAGE" > /dev/null
LOG="${TMPDIR:-/tmp}/rdr208-mvv-$LABEL.log"
ART="${TMPDIR:-/tmp}/rdr208-mvv-$LABEL.artifacts"
rm -rf "$ART"
mkdir -p "$ART"
chmod 777 "$ART"
set +e
docker run --rm -v "$ART:/home/nexus/artifacts" -e MVV_ARTIFACTS=/home/nexus/artifacts \
    -e EXPECT_BRANCH_FIX="$EXPECT" -e MVV_LABEL="$LABEL" "$IMAGE" 2>&1 | tee "$LOG"
set -e
echo "log: $LOG"
echo "artifacts: $ART"
grep -q '^RDR-208 LOCAL-MODE MVV PASSED' "$LOG"
