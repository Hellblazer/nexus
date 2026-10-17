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
# claude plugin eval hands a plugin MCP server only the env its .mcp.json names: NX_NO_TELEMETRY=1
# on the container never reached nx-mcp, and each run, with its own fresh HOME, pinged as a new
# install (nexus-ca5rw; probed on chas 2026-10-17). The extract names it for every server.
python3 - "$DEST/conexus/.mcp.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    config = json.load(f)
for server in config.get("mcpServers", config).values():
    server.setdefault("env", {})["NX_NO_TELEMETRY"] = "1"
with open(path, "w") as f:
    json.dump(config, f, indent=2)
PY
git -C "$WT" rev-parse "$SHA" > "$DEST/commit"
echo "plugins/$ARM/conexus from $(cat "$DEST/commit")"
