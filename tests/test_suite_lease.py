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


# -- the guard fails closed when the lease cannot be taken ------------------------


@pytest.fixture
def conftest_gate(monkeypatch: pytest.MonkeyPatch):
    """The real ``_take_suite_lease`` with an acquire that always loses."""
    gate = sys.modules["tests.conftest"]
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(gate, "_suite_lease_release", None)
    monkeypatch.setattr(_suite_lease, "inside_a_holder", lambda: False)
    monkeypatch.setattr(_suite_lease, "acquire", lambda *a, **k: None)
    monkeypatch.delenv(gate._SUITE_LEASE_UNGUARDED_ENV, raising=False)
    monkeypatch.delenv("NX_SUITE_LEASE_WAIT", raising=False)
    return gate


def test_a_lost_acquire_after_the_wait_refuses_with_75_and_names_the_holder(
    conftest_gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait ran out: holder() is still alive and acquire() returned None.

    The first cut ignored that None and ran the whole suite unguarded, which is
    the overlap the lease exists to stop, reported nowhere.
    """
    monkeypatch.setenv("NX_SUITE_LEASE_WAIT", "1")
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: "pid 4242 (peer-run)")
    with pytest.raises(pytest.exit.Exception) as err:
        conftest_gate._take_suite_lease()
    assert err.value.returncode == 75
    assert "pid 4242 (peer-run)" in err.value.msg
    assert conftest_gate._suite_lease_release is None


def test_losing_the_race_between_the_check_and_the_acquire_also_refuses(
    conftest_gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """holder() saw it free, acquire() lost to another run: still a refusal, wait or no wait."""
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: None)
    with pytest.raises(pytest.exit.Exception) as err:
        conftest_gate._take_suite_lease()
    assert err.value.returncode == 75
    assert "just taken it" in err.value.msg


def test_the_explicit_opt_out_runs_unguarded_and_says_so(
    conftest_gate, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: "pid 4242 (peer-run)")
    monkeypatch.setenv(conftest_gate._SUITE_LEASE_UNGUARDED_ENV, "1")
    conftest_gate._take_suite_lease()  # does not raise
    assert conftest_gate._suite_lease_release is None
    err = capsys.readouterr().err
    assert "UNGUARDED" in err and "pid 4242 (peer-run)" in err


# -- a pid-less lease directory must not wedge the box (round-3 review M1) --------
#
# A kill or a WSL shutdown between mkdir and the pid write, a failed rmdir in
# _release, or a 0700 dir made by another user leaves a lease directory whose pid
# file is missing, empty, non-numeric or unreadable. holder() reads that as free,
# but acquire() used to return None for it forever (reclaim only handled a DEAD
# pid, mkdir saw EEXIST), so with the suite lease failing closed every run would
# wait out its 30 minutes and exit 75 until someone renamed the directory by hand.

_T0 = 1_000_000.0  # a fixed clock: no test below depends on wall time


def _pidless_lease(lease_root: Path, how: str, *, mtime: float = _T0) -> Path:
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    if how == "empty-dir":
        pass
    elif how == "empty-pid":
        (lease / "pid").write_text("")
    elif how == "garbage-pid":
        (lease / "pid").write_text("not-a-pid\n")
    elif how == "label-only":
        (lease / "label").write_text("a run killed before the pid write\n")
    else:  # pragma: no cover - a typo in this test file
        raise AssertionError(how)
    for child in lease.iterdir():
        os.utime(child, (mtime, mtime))
    os.utime(lease, (mtime, mtime))
    return lease


def _clock(at: float):
    return lambda: at


@pytest.mark.parametrize("how", ["empty-dir", "empty-pid", "garbage-pid", "label-only"])
def test_a_pidless_lease_older_than_the_grace_is_reclaimed(lease_root: Path, how: str) -> None:
    lease = _pidless_lease(lease_root, how)
    release = _suite_lease.acquire(
        "after-wedge", lease_root=lease_root, now=_clock(_T0 + _suite_lease.PIDLESS_GRACE_SECONDS + 1))
    assert release is not None, f"a {how} lease wedged the resource past its grace"
    try:
        assert (lease / "pid").read_text().strip() == str(os.getpid())
        assert [p.name for p in lease_root.iterdir() if ".stale." in p.name], "the old directory is set aside, not deleted"
    finally:
        release()


@pytest.mark.parametrize("how", ["empty-dir", "empty-pid", "garbage-pid", "label-only"])
def test_a_pidless_lease_younger_than_the_grace_is_left_alone(lease_root: Path, how: str) -> None:
    """The holder may be between its mkdir and its pid write RIGHT NOW: reclaiming then would be a second holder."""
    _pidless_lease(lease_root, how)
    now = _clock(_T0 + _suite_lease.PIDLESS_GRACE_SECONDS - 1)
    assert _suite_lease.acquire("too-early", lease_root=lease_root, now=now) is None
    assert not [p for p in lease_root.iterdir() if ".stale." in p.name]


def test_the_grace_boundary_is_exactly_the_grace(lease_root: Path) -> None:
    _pidless_lease(lease_root, "empty-dir")
    just_under = _clock(_T0 + _suite_lease.PIDLESS_GRACE_SECONDS - 0.001)
    assert _suite_lease.acquire("x", lease_root=lease_root, now=just_under) is None
    release = _suite_lease.acquire("x", lease_root=lease_root, now=_clock(_T0 + _suite_lease.PIDLESS_GRACE_SECONDS))
    assert release is not None
    release()


def test_the_default_clock_reclaims_a_really_old_pidless_lease(lease_root: Path) -> None:
    """The wiring: with no `now` given, a lease backdated by the real clock is reclaimed."""
    import time

    _pidless_lease(lease_root, "empty-pid", mtime=time.time() - 3600)
    release = _suite_lease.acquire("real-clock", lease_root=lease_root)
    assert release is not None
    release()


def test_a_live_holder_is_never_reclaimed_however_old_the_lease(lease_root: Path) -> None:
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    (lease / "pid").write_text(f"{os.getpid()}\n")
    os.utime(lease, (_T0, _T0))
    os.utime(lease / "pid", (_T0, _T0))
    assert _suite_lease.acquire("thief", lease_root=lease_root, now=_clock(_T0 + 10 * 86400)) is None
    assert (lease / "pid").read_text().strip() == str(os.getpid())


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file, so an unreadable pid file cannot be built")
def test_an_unreadable_pid_file_older_than_the_grace_is_reclaimed(lease_root: Path) -> None:
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    pid = lease / "pid"
    pid.write_text(f"{os.getpid()}\n")  # a LIVE pid we may not read: no holder is knowable from it
    os.utime(pid, (_T0, _T0))
    os.utime(lease, (_T0, _T0))
    pid.chmod(0o000)
    try:
        release = _suite_lease.acquire("x", lease_root=lease_root, now=_clock(_T0 + 120))
        assert release is not None
        release()
    finally:
        for stale in lease_root.glob(f"{_suite_lease.RESOURCE}.stale.*/pid"):
            stale.chmod(0o600)


def test_a_waiting_acquire_succeeds_once_a_pidless_lease_ages_past_the_grace(
        lease_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a wait, the caller no longer waits out the whole timeout on a pid-less corpse."""
    _pidless_lease(lease_root, "empty-dir")
    clock = {"t": _T0 + 10.0}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(_suite_lease.time, "sleep", fake_sleep)
    release = _suite_lease.acquire("waiter", wait_seconds=3600, lease_root=lease_root, now=lambda: clock["t"])
    assert release is not None, "the wait must end when the lease passes its grace, not at the 3600 s deadline"
    assert 40 <= len(sleeps) <= 60, sleeps  # about the 50 seconds that were left of the grace
    release()


def test_the_exit_75_messages_name_the_lease_path_and_the_recovery_command(
        conftest_gate, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A run that cannot take the lease says WHERE it is and how to clear it, on every refusal path."""
    root = tmp_path / "shared-root"
    monkeypatch.setattr(_suite_lease, "_lease_root", lambda: root)
    path = str(root / _suite_lease.RESOURCE)
    # after the wait: a live-looking holder
    monkeypatch.setenv("NX_SUITE_LEASE_WAIT", "1")
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: "pid 4242 (peer-run)")
    with pytest.raises(pytest.exit.Exception) as after_wait:
        conftest_gate._take_suite_lease()
    # the lost race: holder() saw it free
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: None)
    with pytest.raises(pytest.exit.Exception) as lost_race:
        conftest_gate._take_suite_lease()
    # no wait requested and a live holder: the first refusal branch
    monkeypatch.delenv("NX_SUITE_LEASE_WAIT")
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: "pid 4242 (peer-run)")
    with pytest.raises(pytest.exit.Exception) as no_wait:
        conftest_gate._take_suite_lease()
    for err in (after_wait, lost_race, no_wait):
        assert err.value.returncode == 75
        assert path in err.value.msg, err.value.msg
        assert f"rm -rf {path}" in err.value.msg, err.value.msg


# -- the opt-out means exactly "1" (round-3 review L3) -----------------------------


def test_the_opt_out_means_exactly_one_so_a_typo_or_a_false_does_not_disarm_the_guard(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-3 review L3: `false`, `no`, `off` and `true` used to count as set (only "" and "0" were off)."""
    gate = sys.modules["tests.conftest"]
    for value in ("false", "False", "no", "off", "true", "yes", "2", "01", "1 1", "unguarded"):
        monkeypatch.setenv(gate._SUITE_LEASE_UNGUARDED_ENV, value)
        assert gate._suite_lease_unguarded() is False, value
    for value in ("", "0", " 0 "):
        monkeypatch.setenv(gate._SUITE_LEASE_UNGUARDED_ENV, value)
        assert gate._suite_lease_unguarded() is False, value
    monkeypatch.delenv(gate._SUITE_LEASE_UNGUARDED_ENV)
    assert gate._suite_lease_unguarded() is False
    for value in ("1", " 1 ", "1\n"):
        monkeypatch.setenv(gate._SUITE_LEASE_UNGUARDED_ENV, value)
        assert gate._suite_lease_unguarded() is True, repr(value)


def test_a_non_one_value_does_not_lift_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The consumer, not only the predicate: `=off` with a live holder and no wait still exits 75."""
    from tests import _suite_lease

    gate = sys.modules["tests.conftest"]
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(gate, "_suite_lease_release", None)
    monkeypatch.setattr(_suite_lease, "inside_a_holder", lambda: False)
    monkeypatch.setattr(_suite_lease, "holder", lambda *a, **k: "pid 4242 (peer-run)")
    monkeypatch.setattr(_suite_lease, "acquire", lambda *a, **k: None)
    monkeypatch.delenv("NX_SUITE_LEASE_WAIT", raising=False)
    monkeypatch.setenv(gate._SUITE_LEASE_UNGUARDED_ENV, "off")
    with pytest.raises(pytest.exit.Exception) as err:
        gate._take_suite_lease()
    assert err.value.returncode == 75
    assert re.search(r"suite lease: refusing", err.value.msg)
