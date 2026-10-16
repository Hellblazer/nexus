#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Start sr_batch.sh detached on chas (a dropped ssh does not stop it). Same
# arguments as sr_batch.sh. Launch from the laptop with
#   python3 tests/e2e/lib/claude_credentials.py run --remote chas-test -- \
#     bash <wt>/tests/e2e/prefix-capture/ab/skillroute/sr_detach.sh 3 S0 S1 S2
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HERE/out"
setsid -f bash "$HERE/sr_batch.sh" "$@" > "$HERE/out/batch.log" 2>&1 < /dev/null
echo "detached"
