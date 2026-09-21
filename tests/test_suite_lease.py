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
        # An INDEPENDENT run, so scrub the descendant marker this process set
        # when it took the lease above. Leaving it in would exempt the child
        # and this test would assert nothing -- which is the difference
        # between this test and test_a_nested_pytest_run_is_not_refused.
        env.pop(_suite_lease.HELD_BY_ENV, None)
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


# ── a nested pytest is not a second competitor ───────────────────────────────


def test_a_descendant_of_the_holder_is_exempt(lease_root: Path) -> None:
    """The unit-level property: inside a live holder, skip the lease."""
    release = _suite_lease.acquire("outer", lease_root=lease_root)
    assert release is not None
    try:
        assert _suite_lease.inside_a_holder(), (
            "acquire must mark the environment so descendants can tell"
        )
    finally:
        release()
    assert not _suite_lease.inside_a_holder(), "release must clear the marker"


def test_the_exemption_keys_on_a_LIVE_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child outliving its parent is a competitor again, not an heir.

    Non-vacuity for the test above: without the liveness check the exemption
    would be 'the variable is set', which a dead parent leaves behind.
    """
    monkeypatch.setenv(_suite_lease.HELD_BY_ENV, "4194304")  # cannot be alive
    assert not _suite_lease.inside_a_holder()
    monkeypatch.setenv(_suite_lease.HELD_BY_ENV, str(os.getpid()))
    assert _suite_lease.inside_a_holder()


def test_a_nested_pytest_run_is_not_refused(tmp_path: Path) -> None:
    """THE REGRESSION. 19 test files here spawn a nested pytest.

    This is the case dd64caf9d broke on develop within minutes: the inner run
    asked for a lease the outer run held and refused, naming the outer run as
    the holder, so the parent test failed on a missing terminal summary rather
    than on anything it was about.

    The existing refusal test passes a tmp lease root to the child, which
    ALSO scrubs nothing about the environment -- but it never held a real
    marker, so it could not see this. Here the environment is left intact,
    because the marker is the mechanism.
    """
    root = tmp_path / "leases"
    release = _suite_lease.acquire("the-outer-run", lease_root=root)
    assert release is not None
    try:
        env = dict(os.environ)  # marker INCLUDED, unlike the refusal test
        env["NX_BUILD_LEASE_ROOT"] = str(root)
        env.pop("NX_TEST_T2_SUBSTRATE", None)
        out = subprocess.run(
            [sys.executable, "-m", "pytest",
             "tests/test_suite_lease.py::test_a_free_resource_has_no_holder",
             "-q", "-o", "addopts="],
            capture_output=True, text=True, env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
    finally:
        release()

    assert out.returncode == 0, (
        f"a nested run was refused its own parent's lease (rc={out.returncode})\n"
        f"stdout:\n{out.stdout[-1500:]}\nstderr:\n{out.stderr[-1500:]}"
    )
    assert "suite lease: refusing" not in (out.stdout + out.stderr)
