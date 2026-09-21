# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite lease makes one substrate-heavy run per box an enforced fact.

The thing under test is mutual exclusion, so these tests take and release a
lease in a tmp root rather than asserting on code shape. The end-to-end leg
drives a real child pytest, because the property that matters is "a second
run refuses", and only a second run can show that.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _suite_lease


@pytest.fixture
def lease_root(tmp_path: Path) -> Path:
    return tmp_path / "leases"


def test_a_free_resource_has_no_holder(lease_root: Path) -> None:
    assert _suite_lease.holder(lease_root) is None


def test_acquire_then_holder_names_this_process(lease_root: Path) -> None:
    release = _suite_lease.acquire("unit-test", lease_root=lease_root)
    assert release is not None
    try:
        held = _suite_lease.holder(lease_root)
        assert held is not None
        assert str(os.getpid()) in held
        assert "unit-test" in held
    finally:
        release()
    assert _suite_lease.holder(lease_root) is None


def test_a_second_acquire_is_refused_while_the_first_lives(
    lease_root: Path,
) -> None:
    """The whole point. Without this the module is an elaborate no-op."""
    first = _suite_lease.acquire("first", lease_root=lease_root)
    assert first is not None
    try:
        assert _suite_lease.acquire("second", lease_root=lease_root) is None
    finally:
        first()
    # and the resource is takeable again once released
    third = _suite_lease.acquire("third", lease_root=lease_root)
    assert third is not None
    third()


def test_a_dead_holder_does_not_wedge_the_box(lease_root: Path) -> None:
    """A stale lease from a killed run must not block every future run.

    Written with a pid that cannot be alive rather than by killing something,
    so the test does not depend on process timing.
    """
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    # PID 2^22 is above the Linux default pid_max and macOS's ceiling; if it
    # somehow exists the assertion below fails loudly rather than silently
    # passing, which is the right direction for this check.
    (lease / "pid").write_text("4194304\n")
    (lease / "label").write_text("a run that died\n")

    assert _suite_lease.holder(lease_root) is None, (
        "a dead holder must read as free"
    )
    release = _suite_lease.acquire("after-stale", lease_root=lease_root)
    assert release is not None, "a stale lease wedged the resource"
    release()


def test_a_malformed_lease_reads_as_free(lease_root: Path) -> None:
    """A half-written lease must not block the suite on this module's bug."""
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    (lease / "pid").write_text("not-a-pid\n")
    assert _suite_lease.holder(lease_root) is None


def test_a_second_pytest_run_refuses_while_one_holds_the_lease(
    tmp_path: Path,
) -> None:
    """End-to-end: a real child pytest refuses with exit 75 and names the holder.

    This is the leg that would have caught today's incident. The unit tests
    above prove the lease primitive; only this one proves conftest actually
    consults it, which is where the build lease's own asymmetry lived for
    months (read, never taken).
    """
    root = tmp_path / "leases"
    release = _suite_lease.acquire("the-holding-run", lease_root=root)
    assert release is not None
    try:
        # A REAL test path inside the repo tree. A probe file written to
        # tmp_path does not pick up tests/conftest.py at all -- the first
        # version of this test did exactly that, so the child ran happily and
        # the assertion below was checking nothing.
        probe = "tests/test_suite_lease.py::test_a_free_resource_has_no_holder"
        env = dict(os.environ)
        env["NX_BUILD_LEASE_ROOT"] = str(root)
        env.pop("NX_SUITE_LEASE_WAIT", None)
        env.pop("NX_TEST_T2_SUBSTRATE", None)
        out = subprocess.run(
            [sys.executable, "-m", "pytest", probe, "-q", "-o", "addopts="],
            capture_output=True, text=True, env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
    finally:
        release()

    assert out.returncode == 75, (
        f"expected the child run to refuse with 75, got {out.returncode}\n"
        f"stdout:\n{out.stdout[-2000:]}\nstderr:\n{out.stderr[-2000:]}"
    )
    combined = out.stdout + out.stderr
    assert "suite lease" in combined
    assert "the-holding-run" in combined, "the refusal must name the holder"
