#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HERE/out"
setsid -f bash "$HERE/sq_batch.sh" "$@" >> "$HERE/out/batch.log" 2>&1 < /dev/null
echo detached
