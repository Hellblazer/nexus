#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#   export_conexus.sh <arm> <commit>   extract that commit's conexus/ tree
#   into plugins/<arm>/conexus for a tree arm (one capital letter other than
#   S or T): D and N were develop and the narrowed conexus (nexus-dg7i6.2).
#   Run on chas in the worktree; the commit must be fetched there.
#   Replaces any earlier extract.
set -euo pipefail
ARM=${1:?arm letter}; SHA=${2:?commit}
case "$ARM" in [A-RU-Z]) ;; *) echo "arm must be one capital letter other than S or T" >&2; exit 2 ;; esac
HERE=$(cd "$(dirname "$0")" && pwd)
WT=$(cd "$HERE/../../../../.." && pwd)
DEST="$HERE/plugins/$ARM"
git -C "$WT" cat-file -e "$SHA^{tree}"
rm -rf "$DEST/conexus"
mkdir -p "$DEST"
git -C "$WT" archive "$SHA" conexus | tar -x -C "$DEST"
git -C "$WT" rev-parse "$SHA" > "$DEST/commit"
echo "plugins/$ARM/conexus from $(cat "$DEST/commit")"
