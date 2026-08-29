# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Every native-build leg of the migration rehearsal refuses ``--no-build``.

nexus-mbeke: ``--shakeout`` was the ONLY native-build leg in
``tests/e2e/migration-rehearsal/run.sh`` with no ``--no-build`` refusal, so
the PRE-TAG candidate gate — whose whole purpose is "prove THIS candidate
binary" — could be satisfied by whatever stale binary sat in
``service/target``. Its seven siblings refuse at the argument-validation
stage, before the harness lock, the wheel build, the native build, or docker.

This test drives the real script with each leg plus ``--no-build`` and
requires the refusal (exit 2, a message that names the leg and
``--no-build``). Every refusal under test sits BEFORE the RDR-184 harness
lock and before any build or container step, so the invocation is cheap and
side-effect free; a leg whose refusal moved below the lock would show up here
as a hang or a lock message, not a silent pass.

Non-vacuity: the parametrisation is the list of native legs by name, not a
parse of the script, so removing a refusal from the script goes RED here for
that leg (the script would proceed past validation and fail later with a
different exit code and message).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_SH = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "run.sh"

#: Native-build legs whose ``--no-build`` refusal precedes the harness lock.
#: ``--shakeout`` is the nexus-mbeke addition; the rest are its siblings.
#: ``--hole-punch`` / ``--guided`` / ``--cold`` are RETIRED (RDR-155 P4b) and
#: exit on the RETIRED refusal first; the skill-parity test owns that set.
NATIVE_LEGS = [
    "--shakeout",
    "--package-upgrade",
    "--era-hop",
    "--stranded",
    "--candidate-migration",
]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # Belt and braces: even if a refusal ever slid below the lock, the
    # self-test seam exits before the harness body runs.
    env["NX_E2E_LOCK_SELFTEST"] = "1"
    return subprocess.run(
        ["bash", str(RUN_SH), *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env, check=False,
    )


def test_the_harness_exists() -> None:
    assert RUN_SH.is_file(), f"harness moved: {RUN_SH}"


@pytest.mark.parametrize("leg", NATIVE_LEGS)
def test_native_leg_refuses_no_build(leg: str) -> None:
    proc = _run(leg, "--no-build")
    assert proc.returncode == 2, (
        f"{leg} --no-build did not refuse (rc={proc.returncode}); a stale binary "
        f"would satisfy this leg silently.\nstderr:\n{proc.stderr[-800:]}"
    )
    assert "--no-build" in proc.stderr, proc.stderr[-800:]
    assert leg in proc.stderr, proc.stderr[-800:]


def test_shakeout_refusal_names_the_pre_tag_purpose() -> None:
    """The message must say WHY, not just refuse: the pre-tag gate proves the
    candidate binary, so reusing one inverts it (nexus-mbeke)."""
    proc = _run("--shakeout", "--no-build")
    assert proc.returncode == 2
    assert "pre-tag candidate gate" in proc.stderr
    assert "nexus-mbeke" in proc.stderr
