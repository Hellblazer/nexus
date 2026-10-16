#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#   export_conexus.sh <D|N> <commit>   extract that commit's conexus/ tree
#   into plugins/<arm>/conexus: N is the narrowed conexus (nexus-dg7i6.2), D
#   its parent. Run on chas in the worktree; the commit must be fetched there.
#   Replaces any earlier extract.
set -euo pipefail
ARM=${1:?arm D or N}; SHA=${2:?commit}
case "$ARM" in D|N) ;; *) echo "arm must be D or N" >&2; exit 2 ;; esac
HERE=$(cd "$(dirname "$0")" && pwd)
WT=$(cd "$HERE/../../../../.." && pwd)
DEST="$HERE/plugins/$ARM"
git -C "$WT" cat-file -e "$SHA^{tree}"
rm -rf "$DEST/conexus"
mkdir -p "$DEST"
git -C "$WT" archive "$SHA" conexus | tar -x -C "$DEST"
git -C "$WT" rev-parse "$SHA" > "$DEST/commit"
echo "plugins/$ARM/conexus from $(cat "$DEST/commit")"
