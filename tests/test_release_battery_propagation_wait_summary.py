# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""nexus-tt5vm review round 2 (Sam's data-point goal): fresh-install-mvv's
propagation wait logs ``PROPAGATION_WAIT_S=<n>`` when it fires, but the
script's own ``$WORK`` (and every log under it) is deleted on success by
its own cleanup trap -- so the ONE place that data point is guaranteed to
survive past a battery run is ``release-battery.sh``'s own per-leg log
capture (never deleted by the child's cleanup) and the summary table it
prints from ``finish_leg()``.

THE FUNCTION UNDER TEST IS EXTRACTED FROM THE REAL SCRIPT, never retyped
here -- this repo's existing pattern for shape checks over shell (see
``test_release_battery_refuses_moving_tree.py``'s own docstring for why: a
retyped copy passes happily while the original drifts). ``finish_leg()``
has no nested ``{ }`` blocks in its body, so a `/^finish_leg() {/,/^}/`
sed range is a safe, simple extraction; if the function's shape ever grows
one, this test's own harness call below will fail loudly (a bash syntax
error sourcing a truncated function), not silently test nothing.

This is a text-level pin against a synthetic harness, not a full battery
run: `release-battery.sh` itself builds real artifacts and runs many legs,
far too heavy for a unit test. The harness below defines the handful of
associative arrays `finish_leg()` reads/writes and a synthetic leg log,
then calls the REAL extracted function against it.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
BATTERY = REPO_ROOT / "tests" / "e2e" / "release-battery.sh"


def _extract_finish_leg() -> str:
    text = BATTERY.read_text()
    start = text.index("finish_leg() {")
    # The function has no nested `{ }`; its closing brace is the first
    # line that is exactly "}" after the declaration.
    end_marker = "\n}\n"
    end = text.index(end_marker, start)
    body = text[start : end + len(end_marker)]
    assert body.startswith("finish_leg() {"), "extraction anchor drifted"
    assert body.rstrip().endswith("}"), "extraction did not find the closing brace"
    return body


def _run_finish_leg(tmp_path, leg_log_text: str, verdict_regex: str, rc: int = 0) -> str:
    """Run the REAL (extracted) finish_leg() against a synthetic leg log
    and return the resulting summary line (LEG_LINE[leg])."""
    finish_leg_src = _extract_finish_leg()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "mvv.log").write_text(leg_log_text)

    harness = f"""#!/bin/bash
set -euo pipefail
LOGS={logs_dir!s}
declare -A LEG_END LEG_RC LEG_STATUS LEG_LINE LEG_START LEG_VERDICT
LEG_START[mvv]=1000
LEG_VERDICT[mvv]={verdict_regex!r}

{finish_leg_src}

finish_leg mvv {rc} >/dev/null
printf '%s' "${{LEG_LINE[mvv]}}"
"""
    harness_path = tmp_path / "harness.sh"
    harness_path.write_text(harness)
    harness_path.chmod(0o755)
    result = subprocess.run(
        ["bash", str(harness_path)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"harness failed (extraction likely drifted): rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result.stdout


def test_extraction_finds_a_real_function() -> None:
    src = _extract_finish_leg()
    assert "LEG_VERDICT[$leg]" in src
    assert src.count("finish_leg() {") == 1


def test_propagation_wait_folded_into_summary_line(tmp_path) -> None:
    log = (
        "── 1/10 Install PUBLISHED artifact from PyPI (uv-tool resolution layer) ──\n"
        "  PROPAGATION_WAIT_S=42  (uv now resolves conexus==7.99.0; probe attempts=5)\n"
        "FRESH-INSTALL MVV PASSED — conexus 7.99.0 (PUBLISHED artifact, uv-tool resolution layer) [PROPAGATION_WAIT_S=42]\n"
    )
    line = _run_finish_leg(
        tmp_path, log, verdict_regex="FRESH-INSTALL MVV (PASSED|FAILED)", rc=0,
    )
    assert "PASSED" in line
    assert "PROPAGATION_WAIT_S=42" in line
    # Belt-and-suspenders de-dup: the datum already embedded in the
    # sentinel line (this repo's own fresh-install-mvv.sh shape) must not
    # be appended a SECOND time.
    assert line.count("PROPAGATION_WAIT_S=42") == 1, (
        f"the data point was duplicated in the summary line: {line!r}"
    )


def test_no_propagation_wait_line_leaves_summary_unaffected(tmp_path) -> None:
    """Defense in depth (independent of fresh-install-mvv.sh's own output
    shape): if the verdict line does NOT already carry the datum but the
    log has it elsewhere, finish_leg must still fold it in -- this proves
    the battery-side capture does not silently depend on the child
    script's sentinel-line embedding staying correct forever."""
    log = (
        "  PROPAGATION_WAIT_S=17  (uv now resolves conexus==7.99.0; probe attempts=2)\n"
        "FRESH-INSTALL MVV PASSED — conexus 7.99.0 (PUBLISHED artifact, uv-tool resolution layer)\n"
    )
    line = _run_finish_leg(
        tmp_path, log, verdict_regex="FRESH-INSTALL MVV (PASSED|FAILED)", rc=0,
    )
    assert "PASSED" in line
    assert "PROPAGATION_WAIT_S=17" in line


def test_other_legs_unaffected_no_propagation_wait_in_log(tmp_path) -> None:
    """A leg whose output never contains the literal (every leg besides
    mvv) must see its summary line completely unchanged -- the fold-in is
    a no-op there, never a spurious empty bracket."""
    log = "SHAKEDOWN PASSED\n"
    line = _run_finish_leg(tmp_path, log, verdict_regex="SHAKEDOWN (PASSED|FAILED)", rc=0)
    assert line == "SHAKEDOWN PASSED"
    assert "PROPAGATION_WAIT_S" not in line
