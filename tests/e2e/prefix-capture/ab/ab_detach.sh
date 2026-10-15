#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-ly9c2: start ab_batch.sh detached on chas (so a dropped ssh does not
# stop it). Same arguments as ab_batch.sh. Launch from the laptop with
#   python3 tests/e2e/lib/claude_credentials.py run --remote chas-test -- \
#     bash <wt>/tests/e2e/prefix-capture/ab/ab_detach.sh <ARM> <REPS> <task>...
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HERE/out"
setsid -f bash "$HERE/ab_batch.sh" "$@" > "$HERE/out/$1.log" 2>&1 < /dev/null
echo "detached $1"
