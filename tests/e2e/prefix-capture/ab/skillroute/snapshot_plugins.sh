#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Laptop side: copy the distractor plugins for the skill-routing arms into
# ab/skillroute/plugins/ (not committed), from this user's installed plugin
# cache. S1 = beads; S2 = every enabled plugin except conexus and sn (the image
# already carries those) and the LSP plugins (no skills, need host binaries).
# Then rsync the directory to the chas worktree before a batch.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
INSTALLED=${INSTALLED:-$HOME/.claude/plugins/installed_plugins.json}
SETTINGS=${SETTINGS:-$HOME/.claude/settings.json}
DEST=$HERE/plugins
rm -rf "$DEST" && mkdir -p "$DEST/S1" "$DEST/S2"

path_of() { jq -r --arg k "$1" '.plugins[$k][0].installPath // empty' "$INSTALLED"; }

BEADS=$(path_of "beads@beads-marketplace")
[ -d "$BEADS" ] || { echo "beads plugin not installed" >&2; exit 1; }
cp -R "$BEADS" "$DEST/S1/beads"

jq -r '.enabledPlugins | to_entries[] | select(.value == true) | .key' "$SETTINGS" |
while read -r key; do
  case "$key" in conexus@*|sn@*|*-lsp@*) continue ;; esac
  src=$(path_of "$key")
  [ -d "$src" ] || { echo "skip $key: no install path" >&2; continue; }
  cp -R "$src" "$DEST/S2/${key%@*}"
done
ls "$DEST/S1" "$DEST/S2"
