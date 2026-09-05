# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/e2e/lib/exit_diagnostics.sh -- the silent-exit guard (nexus-f2g8u).

tests/e2e/migration-rehearsal/run.sh was observed to exit non-zero after a
SUCCESSFUL wheel build with zero output on either stream (2026-09-02,
NEXUS_TARGET_RELEASE=7.27.0 --package-upgrade). The fix arms an ERR trap
(propagated into functions via `set -o errtrace`) that records the last
failing command's line/text, and an EXIT-trap guard (`diag_exit_guard`,
chained first into every `trap '...' EXIT` run.sh installs) that prints
that record to stderr whenever the script is about to exit non-zero.

This does NOT run the rehearsal, Docker, or a real wheel build. It proves
the mechanism itself by mutation (inject a real command failure in a
throwaway harness that sources the ACTUAL lib file and wires it exactly as
run.sh does, then asserts the diagnostic names the right line/command) and
proves run.sh is actually wired to it (grep-based, mirroring
tests/e2e/lib/harness_lock_test.sh's established non-vacuity pattern for
this repo's other shared shell libs).

Non-vacuity: TestMechanismFiresOnFailure's assertion is FALSE (not merely
absent-of-signal) with the chaining removed --
TestMechanismIsVacuousWithoutWiring proves this directly by running the
identical harness minus the `diag_arm_err_trap` / EXIT-trap chain and
asserting the diagnostic text does NOT appear. Deleting the wiring from
run.sh itself is caught separately by TestRunShWiring, since the harness
test does not touch run.sh's own source.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "tests" / "e2e" / "lib" / "exit_diagnostics.sh"
RUN_SH = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "run.sh"


def _harness(tmp_path: Path, *, wire_it: bool) -> Path:
    """A throwaway script mirroring run.sh's exact wiring shape.

    `failing_pipeline` reproduces the real bug's shape verbatim: a
    function whose body assigns from a failing pipeline under
    `set -euo pipefail` (matching run.sh's preflight_docker_prune, whose
    `docker system df ... | awk ...` assignment is the confirmed silent-
    death path) -- never a bare top-level `false`, so the test exercises
    the errtrace-into-functions behavior the fix specifically depends on.
    """
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f'source "{LIB}"',
    ]
    if wire_it:
        lines += [
            "diag_arm_err_trap",
            "trap 'diag_exit_guard' EXIT",
        ]
    else:
        # Deliberately the "trap removed" mutant: the lib is sourced (so a
        # source-not-found failure can't masquerade as this case) but
        # never armed/chained -- exactly what a regression stripping the
        # run.sh wiring would look like.
        lines += ["trap true EXIT"]
    lines += [
        "failing_pipeline() {",
        "  local x",
        '  x="$(false 2>/dev/null | cat)"',  # <- known line, asserted below
        '  echo "unreachable: $x"',
        "}",
        "failing_pipeline",
    ]
    script = tmp_path / ("wired.sh" if wire_it else "unwired.sh")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


def _failing_line(script: Path) -> int:
    for i, line in enumerate(script.read_text().splitlines(), 1):
        if "false 2>/dev/null" in line:
            return i
    raise AssertionError("harness fixture drifted: no failing-pipeline line found")


def _run(script: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "NO_COLOR": "1"}
    return subprocess.run(
        ["bash", str(script)], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=30,
    )


class TestLibExists:
    def test_lib_file_exists(self) -> None:
        assert LIB.exists(), f"missing: {LIB}"


class TestMechanismFiresOnFailure:
    """Mutation: a real, uncaught command failure inside a function must
    produce a FATAL diagnostic naming the exact failing line and command."""

    def test_diagnostic_names_failing_line_and_command(self, tmp_path) -> None:
        script = _harness(tmp_path, wire_it=True)
        expected_line = _failing_line(script)

        result = _run(script)

        assert result.returncode == 1, result.stderr
        assert "unreachable" not in result.stdout, "pipeline failure did not abort the function"
        assert f"line {expected_line}:" in result.stderr, result.stderr
        assert "false" in result.stderr, result.stderr
        assert result.stderr.strip().startswith("FATAL:") or "FATAL:" in result.stderr

    def test_exit_code_is_preserved_through_the_trap(self, tmp_path) -> None:
        # diag_exit_guard must never itself call `exit` -- if it did, the
        # trap's own status (0, since echo succeeds) would silently
        # overwrite the real failure's exit code.
        script = _harness(tmp_path, wire_it=True)
        result = _run(script)
        assert result.returncode == 1


class TestMechanismIsVacuousWithoutWiring:
    """Non-vacuity proof: the identical failure, through the identical
    lib, with only the arm/chain calls removed, produces NO diagnostic --
    so TestMechanismFiresOnFailure's positive assertion is a real signal,
    not something that would pass regardless of the wiring."""

    def test_no_diagnostic_when_trap_not_chained(self, tmp_path) -> None:
        script = _harness(tmp_path, wire_it=False)
        result = _run(script)
        assert result.returncode == 1  # errexit still kills it; just silently
        assert "FATAL:" not in result.stderr, result.stderr
        assert result.stderr == "", result.stderr


class TestRunShWiring:
    """run.sh itself must actually be wired to the lib -- catches a
    regression that reverts run.sh's edits while leaving the lib intact
    (which TestMechanismFiresOnFailure alone cannot see, since it never
    touches run.sh's own source)."""

    @pytest.fixture(scope="class")
    def run_sh_text(self) -> str:
        assert RUN_SH.exists(), f"missing: {RUN_SH}"
        return RUN_SH.read_text()

    def test_sources_the_lib(self, run_sh_text: str) -> None:
        assert "lib/exit_diagnostics.sh" in run_sh_text

    def test_arms_the_err_trap(self, run_sh_text: str) -> None:
        assert re.search(r"^diag_arm_err_trap$", run_sh_text, re.MULTILINE), (
            "run.sh no longer calls diag_arm_err_trap"
        )

    def test_every_exit_trap_chains_the_guard_first(self, run_sh_text: str) -> None:
        # Comment-stripped so a doc comment that happens to mention
        # `trap '...' EXIT` cannot masquerade as a real installation.
        code_only = "\n".join(
            line for line in run_sh_text.splitlines()
            if not line.strip().startswith("#")
        )
        exit_traps = re.findall(r"trap\s+'([^']*)'\s+EXIT", code_only)
        assert len(exit_traps) >= 5, (
            f"expected at least 5 `trap '...' EXIT` installs in run.sh, found "
            f"{len(exit_traps)} -- this test's count assumption needs updating "
            f"if that's an intentional refactor"
        )
        not_chained = [t for t in exit_traps if not t.strip().startswith("diag_exit_guard")]
        assert not not_chained, (
            "these run.sh EXIT traps do not chain diag_exit_guard first: "
            f"{not_chained}"
        )
