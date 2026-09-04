# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1c7oq: the no-bare-green advisory, producer and consumer composed."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.gate_advisory import (
    PASSED_BY_DEFAULT_PREFIX,
    count_passed_by_default,
    passed_by_default,
)

REPO_ROOT = Path(__file__).parent.parent
SHELL_LIB = REPO_ROOT / "tests" / "e2e" / "lib" / "gate_advisory.sh"


def test_line_shape_and_round_trip() -> None:
    line = passed_by_default("check_release_ci_evidence", "evidence borrowed from the PR head")
    assert line == "GATE PASSED-BY-DEFAULT: check_release_ci_evidence evidence borrowed from the PR head"
    assert count_passed_by_default(f"noise\n{line}\nmore\n") == 1
    assert count_passed_by_default("GATE PASSED: x\nGATE  PASSED-BY-DEFAULT: y z\n") == 0, "the prefix is exact"


def test_a_gate_without_a_reason_is_refused() -> None:
    with pytest.raises(ValueError):
        passed_by_default("gate", "  ")


def test_the_shell_producer_writes_the_same_prefix() -> None:
    """The bash copy of the producer and the Python consumer share one
    literal; the shell line is fed to the Python counter."""
    out = subprocess.run(
        ["bash", "-c", f'source "{SHELL_LIB}"; passed_by_default my-gate "key masked, subset skipped"; echo "n=$GATE_PASSED_BY_DEFAULT"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert count_passed_by_default(out) == 1
    assert "n=1" in out
    assert PASSED_BY_DEFAULT_PREFIX in SHELL_LIB.read_text(), "the shell literal must be the Python one verbatim"
