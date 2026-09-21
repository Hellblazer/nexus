# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite lease makes one substrate-heavy run per box an enforced fact.

The thing under test is mutual exclusion, so these tests take and release a
lease in a tmp root rather than asserting on code shape. The end-to-end leg
drives a real child pytest, because the property that matters is "a second
run refuses", and only a second run can show that.
"""
from __future__ import annotations

import os
import re
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

    combined = out.stdout + out.stderr

    # THE CLAIM, asserted on the refusal's OWN signature and nothing else.
    # It runs on every path, whatever the child's exit code, so the skip
    # below can never carry it away.
    #
    # The first cut asserted ``out.returncode == 0`` under a message naming a
    # lease refusal, which attributed ANY nonzero rc to this guard. Under a
    # full ``-n auto`` run the child exits 1 from a HikariPool
    # ConnectException -- it cannot reach its substrate under the outer run's
    # resource pressure -- and the message said "refused its own parent's
    # lease" about a substrate failure it never looked at. Found by nexus-34,
    # reproduced on two trees; it passes alone and under -n 4 alone, so the
    # misattribution only ever surfaced where the stdout was longest.
    #
    # NON-VACUITY FIRST (nexus-moht0). The lease decision happens in the
    # child's ``pytest_sessionstart``, BEFORE any substrate boots, so a child
    # that dies later of a substrate failure has still answered the question
    # this test asks. A child that dies EARLIER -- a conftest ImportError, a
    # usage error, the wrong interpreter -- never reached the lease logic at
    # all, and its clean-looking absence of a refusal line proves nothing.
    # That distinction is the whole guard: without it the assertion below
    # passes hardest exactly when the child ran least.
    #
    # Asserted as POSITIVE EVIDENCE that the session started, never as a list
    # of ways it might not have. The first cut of this guard enumerated two
    # failure shapes -- a conftest ImportError and exit 4 -- and was checked
    # against a real dead child that produced NEITHER: a system interpreter
    # with no pytest at all exits 1 with "No module named pytest", sails past
    # both, and lands on a clean-looking absence of a refusal line. Two
    # observed shapes, a blocklist covering one. Enumerating failure modes is
    # a reconstruction and inherits the usual tax; requiring the run to show
    # it got somewhere is a read.
    #
    # A summary line means collection completed, which means
    # ``pytest_sessionstart`` -- where the lease is taken -- has already run.
    # A child that dies AFTER that (the HikariPool ConnectException under
    # load) has still answered this test's question; one that dies before it
    # never asked.
    assert re.search(r"\d+ (passed|failed|error|skipped|deselected)", combined), (
        "the child never reached a pytest summary line, so it never got to "
        "pytest_sessionstart and never asked for a lease -- a missing refusal "
        "line here is not evidence of anything (wrong interpreter? no pytest?)"
        f"\nrc={out.returncode}\nstdout:\n{out.stdout[-1500:]}"
        f"\nstderr:\n{out.stderr[-1500:]}"
    )

    # THE CLAIM. The refusal LINE is the signature, not the exit code: exit
    # 75 is shared with ``_gate_on_build_lease`` (tests/conftest.py:342-347),
    # which refuses a DIFFERENT resource, so keying on the code alone would
    # blame the suite lease for a build-lease refusal. The two are only
    # separable today because NX_BUILD_LEASE_ROOT isolates both leases to a
    # fresh tmp dir here -- an accident of the fixture, not something an
    # assertion on rc could defend. The line is unambiguous and is always
    # emitted with the refusal (verified: lease held, marker scrubbed, child
    # exits 75 AND prints it).
    #
    # No skip branch, deliberately. The first cut skipped on any nonzero rc
    # that was not a refusal, which under sustained load -- the exact
    # condition where dd64caf9d's regression resurfaces -- would have skipped
    # every run with nothing counting the skips.
    assert "suite lease: refusing" not in combined, (
        "a nested run hit the suite-lease refusal path\n"
        f"stdout:\n{out.stdout[-1500:]}\nstderr:\n{out.stderr[-1500:]}"
    )
