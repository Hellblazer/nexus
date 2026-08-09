#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Manual recovery for stale nexus_t2_substrate_pg_* clusters left behind by
# finished pytest sessions (nexus-ui654). A heavy multi-agent session can
# accumulate dozens of these tmpdirs even though the processes themselves
# DO reap on clean exit (bead REFINED PICTURE, 2026-08-09) -- the dirs are
# debris, and ensure_engine()'s own startup sweep (nexus-lgdy1) only runs
# the NEXT time a pytest process boots a substrate. This script runs that
# same sweep on demand, without waiting for a new pytest session.
#
# It is a THIN WRAPPER around tests._engine_substrate.sweep_stale_substrate_
# clusters() -- the exact dead-owner identity check ensure_engine() runs
# automatically at the top of every _boot() -- invoked here against the
# box's REAL tempdir root instead of a test's throwaway tmp_path. No
# separate kill/identity logic is reimplemented in bash: the Python sweep
# already (a) writes a pidfile-equivalent sidecar per cluster recording the
# owning pytest PID + exact cmdline, (b) refuses to touch a cluster whose
# owner is still alive and cmdline-matches (PID-reuse guard), (c) verifies
# BOTH the engine and postmaster legs before killing either, and (d) only
# ever reports -- never auto-kills -- a pre-sidecar legacy cluster it
# cannot prove is stale. See tests/_engine_substrate.py's own module
# docstring and tests/test_engine_substrate_sweep.py for the full contract
# and its test coverage.
#
# No ambient state assumptions (gates-scripted-not-ambient discipline):
# everything this needs -- the stray-cluster glob, each cluster's sidecar,
# each recorded PID's actual current liveness/cmdline -- is read fresh from
# disk/proc on every invocation, nothing cached or asserted in advance.
#
# Usage:
#   scripts/sweep-test-substrates.sh
#
# Exit code: 0 on a completed sweep (even if nothing was found to reap --
# an empty machine is a successful sweep, not a failure). Non-zero only if
# the sweep itself could not run at all (e.g. this checkout's `tests`
# package is not importable from here).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

echo "nexus-ui654: sweeping stale nexus_t2_substrate_pg_* clusters (real tempdir root)"

uv run python - <<'PY'
import sys
import tempfile
import warnings
from pathlib import Path

# Never boots anything, never used to select a T2 backend -- imported
# purely for the sweep function and glob constant. Matches this file's own
# fail-loud-if-unimportable contract: an ImportError here is a real
# checkout problem, not something to swallow.
from tests._engine_substrate import _STRAY_GLOB, sweep_stale_substrate_clusters

root = Path(tempfile.gettempdir())
print(f"root: {root}")
print(f"pattern: {_STRAY_GLOB}")

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    result = sweep_stale_substrate_clusters(tmp_root=root)

for w in caught:
    print(f"  {w.category.__name__}: {w.message}")

def _bucket(label: str, items: list[str]) -> None:
    print(f"{label}: {len(items)}")
    for item in items:
        print(f"  - {item}")

_bucket("reaped (dead owner, killed + removed)", result.reaped)
_bucket("live_untouched (owner still running -- refused)", result.live_untouched)
_bucket("mismatch_refused (PID-reuse guard -- left for manual review)", result.mismatch_refused)
_bucket("legacy_reported (pre-sidecar, cannot prove staleness -- see message above)", result.legacy_reported)
_bucket("errors (sweep could not classify -- left untouched)", result.errors)

total_found = (
    len(result.reaped) + len(result.live_untouched)
    + len(result.mismatch_refused) + len(result.legacy_reported)
    + len(result.errors)
)
if total_found == 0:
    print("nothing found -- clean")
else:
    print(
        f"swept {total_found} cluster(s): {len(result.reaped)} reaped, "
        f"{len(result.live_untouched)} live (untouched), "
        f"{len(result.mismatch_refused)} PID-reuse-refused, "
        f"{len(result.legacy_reported)} legacy (reported only), "
        f"{len(result.errors)} errored"
    )
    if result.legacy_reported:
        print(
            "legacy clusters were only REPORTED, never auto-reaped "
            "-- see the warning text above for the manual pg_ctl/rm "
            "command per cluster (tests/_engine_substrate.py's "
            "_sweep_legacy_cluster docstring explains why)."
        )
PY

echo "nexus-ui654: sweep complete"
