#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# RDR-120 stress harness runner.
#
# Per the 2026-05-21 RDR-120 amendment, each phase's merge gates on
# the relevant tests/stress/test_*_stress.py file passing in CI plus
# 24h of shakedown on main. This wrapper runs whichever scenario
# files apply to the phase under test.
#
# Usage:
#   scripts/rdr120_stress.sh                # all stress suites
#   scripts/rdr120_stress.sh t3             # T3 daemon scenarios (P2 gate)
#   scripts/rdr120_stress.sh t2             # T2 daemon scenarios (P3a gate)
#
# Exit codes:
#   0  all scenarios green
#   N  pytest's own exit code (1=failed, 2=usage, etc.)

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

target="${1:-all}"

case "$target" in
    all)
        paths="tests/stress/"
        ;;
    t3)
        paths="tests/stress/test_t3_daemon_stress.py"
        ;;
    t2)
        paths="tests/stress/test_t2_daemon_stress.py"
        ;;
    *)
        echo "usage: $0 [all|t3|t2]" >&2
        exit 2
        ;;
esac

echo "[stress] running: $paths"
exec uv run pytest -m stress "$paths" -v
