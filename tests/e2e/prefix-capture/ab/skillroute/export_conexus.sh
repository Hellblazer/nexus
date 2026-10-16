#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#   export_conexus.sh <commit>   extract that commit's conexus/ tree into
#   plugins/N/conexus for arm N (nexus-dg7i6.2). Run on chas in the worktree;
#   the commit must be fetched there. Replaces any earlier extract.
set -euo pipefail
SHA=${1:?commit}
HERE=$(cd "$(dirname "$0")" && pwd)
WT=$(cd "$HERE/../../../../.." && pwd)
DEST="$HERE/plugins/N"
git -C "$WT" cat-file -e "$SHA^{tree}"
rm -rf "$DEST/conexus"
mkdir -p "$DEST"
git -C "$WT" archive "$SHA" conexus | tar -x -C "$DEST"
git -C "$WT" rev-parse "$SHA" > "$DEST/commit"
echo "plugins/N/conexus from $(cat "$DEST/commit")"
