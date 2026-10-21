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
from tests._module_seam import module_time, setattr_in


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
        assert not [p.name for p in lease_root.iterdir() if ".stale." in p.name], "the set-aside directory is removed (nexus-3tx9c)"
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


# -- an UNREADABLE lease is HELD, never reclaimed (round-4 review M1) ---------------
#
# Round 4 reclaimed an unreadable pid file after the grace, as if it were a corpse.
# A live holder whose lease a peer cannot read (a peer with umask 077: dir 0700, pid
# 0600) lost its lease after 60 s while its suite kept running: two -n 12 suites on
# one box, silently. Only a MISSING, EMPTY or GARBAGE pid file is a corpse.


@pytest.fixture
def live_holder_with_unreadable_pid(lease_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A lease whose pid names THIS (live) process, but whose pid file raises EACCES for the reader.

    Built by making ``Path.read_text`` raise on that one file, so it runs as root too (root reads
    through a chmod 000, which is why the real-chmod tests below skip there).
    """
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    pid = lease / "pid"
    pid.write_text(f"{os.getpid()}\n")
    os.utime(pid, (_T0, _T0))
    os.utime(lease, (_T0, _T0))
    real = Path.read_text

    def read_text(self: Path, *a, **k):
        if self == pid:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    return lease


def test_a_live_holder_with_an_unreadable_pid_file_is_not_reclaimed_two_minutes_on(
        lease_root: Path, live_holder_with_unreadable_pid: Path) -> None:
    lease = live_holder_with_unreadable_pid
    assert _suite_lease.acquire("thief", lease_root=lease_root, now=_clock(_T0 + 120)) is None
    assert not [p for p in lease_root.iterdir() if ".stale." in p.name], "the lease was set aside"
    assert lease.is_dir()


def test_an_unreadable_pid_file_is_not_reclaimed_however_old_the_lease(
        lease_root: Path, live_holder_with_unreadable_pid: Path) -> None:
    assert _suite_lease.acquire("thief", lease_root=lease_root, now=_clock(_T0 + 30 * 86400)) is None


def test_holder_reports_an_unreadable_lease_as_held_not_free(
        lease_root: Path, live_holder_with_unreadable_pid: Path) -> None:
    """The pre-check in conftest uses holder(): None there means "free", which would skip the 75 message."""
    held = _suite_lease.holder(lease_root)
    assert held is not None and "permission denied" in held


def test_a_lease_that_cannot_be_taken_because_it_is_unreadable_exits_75_naming_the_path_and_remedy(
        conftest_gate, monkeypatch: pytest.MonkeyPatch, lease_root: Path,
        live_holder_with_unreadable_pid: Path) -> None:
    monkeypatch.setattr(_suite_lease, "_lease_root", lambda: lease_root)
    monkeypatch.setattr(_suite_lease, "acquire", _real_acquire)
    with pytest.raises(pytest.exit.Exception) as err:
        conftest_gate._take_suite_lease()
    assert err.value.returncode == 75
    path = str(lease_root / _suite_lease.RESOURCE)
    assert path in err.value.msg and f"rm -rf {path}" in err.value.msg and "permission denied" in err.value.msg


_real_acquire = _suite_lease.acquire


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file, so an unreadable pid file cannot be built")
def test_a_pid_file_chmod_000_is_held_not_reclaimed_with_the_real_filesystem(lease_root: Path) -> None:
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    pid = lease / "pid"
    pid.write_text(f"{os.getpid()}\n")
    os.utime(pid, (_T0, _T0))
    os.utime(lease, (_T0, _T0))
    pid.chmod(0o000)
    try:
        assert _suite_lease.acquire("x", lease_root=lease_root, now=_clock(_T0 + 120)) is None
        assert _suite_lease.holder(lease_root) is not None
    finally:
        pid.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches every directory, so an unreadable lease dir cannot be built")
def test_a_lease_dir_chmod_000_is_held_not_reclaimed_with_the_real_filesystem(lease_root: Path) -> None:
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    (lease / "pid").write_text(f"{os.getpid()}\n")
    os.utime(lease / "pid", (_T0, _T0))
    os.utime(lease, (_T0, _T0))
    lease.chmod(0o000)
    try:
        assert _suite_lease.acquire("x", lease_root=lease_root, now=_clock(_T0 + 120)) is None
        assert _suite_lease.holder(lease_root) is not None
    finally:
        lease.chmod(0o700)


def test_a_new_lease_is_readable_by_peers_whatever_the_umask(lease_root: Path) -> None:
    """chmod after create, not umask: a umask-077 holder must stay readable so the next run sees it as live."""
    old = os.umask(0o077)
    try:
        release = _suite_lease.acquire("private-umask", lease_root=lease_root)
    finally:
        os.umask(old)
    assert release is not None
    try:
        lease = lease_root / _suite_lease.RESOURCE
        assert (lease.stat().st_mode & 0o755) == 0o755, oct(lease.stat().st_mode)
        for name in ("pid", "label"):
            assert (lease / name).stat().st_mode & 0o644 == 0o644, (name, oct((lease / name).stat().st_mode))
    finally:
        release()


def test_a_failing_chmod_still_leaves_the_pid_written(
        lease_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-5 review I1: chmod raising must not skip the pid write, or the live holder reads pid-less and is stolen."""
    def boom(self: Path, *_a: object, **_k: object) -> None:
        raise PermissionError("chmod refused")

    monkeypatch.setattr(Path, "chmod", boom)
    release = _suite_lease.acquire("chmod-fails", lease_root=lease_root)
    assert release is not None
    try:
        pid_file = lease_root / _suite_lease.RESOURCE / "pid"
        assert pid_file.read_text().strip() == str(os.getpid())
    finally:
        monkeypatch.undo()
        release()


def test_the_recovery_hint_offers_a_cross_user_mv_not_only_rm(lease_root: Path) -> None:
    """Round-5 review I2: rm -rf of another user's 0755 lease fails; mv in a group-writable, non-sticky root works."""
    hint = _suite_lease.recovery_hint(lease_root)
    path = str(lease_root / _suite_lease.RESOURCE)
    assert f"mv {path} {path}.stale-" in hint


def test_a_waiting_acquire_succeeds_once_a_pidless_lease_ages_past_the_grace(
        lease_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a wait, the caller no longer waits out the whole timeout on a pid-less corpse."""
    _pidless_lease(lease_root, "empty-dir")
    clock = {"t": _T0 + 10.0}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    module_time(monkeypatch, _suite_lease).sleep = fake_sleep
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


# nexus-3tx9c: a reclaimed lease was renamed to suite.stale.<pid>.<ns> and never
# removed (17 on one box), and a corpse made by one user could not be deleted by
# the other. The rename stays the atomic step that picks the winner; the removal
# after it is best-effort, a sweep at acquire catches what an interrupted removal
# left, and the lease is group-writable so either user in the shared group can clear
# the other's corpse.

_HOUR = _suite_lease.STALE_SWEEP_AGE_SECONDS


def _dead_lease(lease_root: Path) -> Path:
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    (lease / "pid").write_text("4194304\n")  # above pid_max, see test_a_dead_holder_does_not_wedge_the_box
    (lease / "label").write_text("a run that died\n")
    return lease


def _stale_dirs(lease_root: Path) -> list[str]:
    return sorted(p.name for p in lease_root.iterdir() if p.name.startswith(f"{_suite_lease.RESOURCE}.stale."))


def _leftover(lease_root: Path, name: str, mtime: float) -> Path:
    old = lease_root / name
    old.mkdir(parents=True, exist_ok=True)
    (old / "pid").write_text("4194304\n")
    os.utime(old, (mtime, mtime))
    return old


def test_the_set_aside_directory_is_gone_after_a_reclaim(lease_root: Path) -> None:
    _dead_lease(lease_root)
    release = _suite_lease.acquire("after-reclaim", lease_root=lease_root)
    assert release is not None
    try:
        assert _stale_dirs(lease_root) == [], "the reclaimed lease was renamed aside and left behind"
        assert (lease_root / _suite_lease.RESOURCE / "pid").read_text().strip() == str(os.getpid())
    finally:
        release()


def test_a_removal_failure_neither_raises_nor_blocks_the_acquire(
        lease_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dead_lease(lease_root)

    def boom(*_a: object, **_k: object) -> None:
        raise PermissionError("owned by the other user")

    setattr_in(monkeypatch, _suite_lease, "shutil.rmtree", boom)
    release = _suite_lease.acquire("removal-fails", lease_root=lease_root)
    assert release is not None, "a failed cleanup of the corpse must not cost the run its lease"
    try:
        assert len(_stale_dirs(lease_root)) == 1, "the rename is the step that decides the winner and it still happened"
    finally:
        release()


def test_an_old_stale_directory_is_swept_and_a_young_one_kept(lease_root: Path) -> None:
    old = _leftover(lease_root, "suite.stale.111.1", _T0)
    young = _leftover(lease_root, "suite.stale.222.2", _T0 + _HOUR - 1)
    release = _suite_lease.acquire("sweeper", lease_root=lease_root, now=_clock(_T0 + _HOUR))
    assert release is not None
    try:
        assert not old.exists(), "a leftover at least an hour old is removed"
        assert young.exists(), "a leftover younger than the age cutoff may still be mid-removal by its owner"
    finally:
        release()


def test_the_sweep_leaves_the_live_lease_and_unrelated_siblings_alone(lease_root: Path) -> None:
    live = lease_root / _suite_lease.RESOURCE
    live.mkdir(parents=True)
    (live / "pid").write_text(f"{os.getpid()}\n")
    os.utime(live, (_T0, _T0))
    other = lease_root / "service"
    other.mkdir()
    os.utime(other, (_T0, _T0))
    _leftover(lease_root, "suite.stale.333.3", _T0)
    # held by this live process, so acquire refuses; the sweep still ran first.
    assert _suite_lease.acquire("held", lease_root=lease_root, now=_clock(_T0 + 10 * _HOUR)) is None
    assert (live / "pid").read_text().strip() == str(os.getpid()), "the live lease was touched"
    assert other.is_dir(), "a sibling that is not a suite.stale.* directory was touched"
    assert _stale_dirs(lease_root) == []


def test_the_sweep_does_not_follow_a_symlink_out_of_the_lease_root(lease_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious"
    precious.write_text("keep\n")
    lease_root.mkdir()
    link = lease_root / "suite.stale.444.4"
    link.symlink_to(outside, target_is_directory=True)
    release = _suite_lease.acquire("symlink", lease_root=lease_root, now=_clock(_T0 + 1000 * _HOUR))
    assert release is not None
    try:
        assert precious.read_text() == "keep\n", "the sweep followed a symlink and deleted its target's contents"
        assert outside.is_dir()
        assert link.is_symlink(), "the sweep touched the link itself"
    finally:
        release()


def test_a_new_lease_is_group_writable_and_never_world_writable_whatever_the_umask(lease_root: Path) -> None:
    """Either user in the shared group must be able to clear the other's corpse, so the group bit is set explicitly."""
    old = os.umask(0o077)
    try:
        release = _suite_lease.acquire("group-writable", lease_root=lease_root)
    finally:
        os.umask(old)
    assert release is not None
    try:
        lease = lease_root / _suite_lease.RESOURCE
        assert lease.stat().st_mode & 0o775 == 0o775, oct(lease.stat().st_mode)
        assert lease.stat().st_mode & 0o002 == 0, "the lease directory is world-writable"
        for name in ("pid", "label"):
            mode = (lease / name).stat().st_mode
            assert mode & 0o664 == 0o664, (name, oct(mode))
            assert mode & 0o002 == 0, (name, "world-writable", oct(mode))
    finally:
        release()


# -- lease telemetry (nexus-jqk5c): one key=value line per contended event -----------
#
# Only contention is logged: an immediate acquire writes nothing, so the log is the
# record of waits and refusals labrun measures. Best-effort throughout.

_EVENT_RE = re.compile(
    r"^ts=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z lease=suite name=suite host=\S+ pid=(?P<pid>\d+) "
    r"waited_s=(?P<waited>\d+) outcome=(?P<outcome>[a-z_]+)(?P<rest>.*)$"
)


@pytest.fixture
def events_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A per-test log; never the real per-host one."""
    log = tmp_path / "state" / "lease-events.log"
    monkeypatch.setenv(_suite_lease.EVENTS_LOG_ENV, str(log))
    return log


def _events(log: Path) -> list[re.Match[str]]:
    if not log.exists():
        return []
    lines = log.read_text().splitlines()
    parsed = [_EVENT_RE.match(line) for line in lines]
    assert all(parsed), lines
    return [m for m in parsed if m]


def _fake_clock(monkeypatch: pytest.MonkeyPatch, on_sleep=None) -> dict[str, float]:
    """A monotonic clock that only advances when the (fake) sleep is called."""
    state = {"t": 5_000.0}

    def fake_sleep(seconds: float) -> None:
        state["t"] += seconds
        if on_sleep is not None:
            on_sleep(state)

    proxy = module_time(monkeypatch, _suite_lease)
    proxy.sleep = fake_sleep
    proxy.monotonic = lambda: state["t"]
    return state


def test_an_immediate_acquire_writes_no_event(lease_root: Path, events_log: Path) -> None:
    release = _suite_lease.acquire("quiet", lease_root=lease_root)
    assert release is not None
    release()
    assert not events_log.exists()
    release = _suite_lease.acquire("quiet-with-wait", wait_seconds=60, lease_root=lease_root)
    assert release is not None
    release()
    assert not events_log.exists()


def test_a_refusal_with_no_wait_logs_one_refused_line_naming_the_holder(lease_root: Path, events_log: Path) -> None:
    first = _suite_lease.acquire("peer run: pytest -n auto", lease_root=lease_root)
    assert first is not None
    try:
        assert _suite_lease.acquire("second", lease_root=lease_root) is None
    finally:
        first()
    (event,) = _events(events_log)
    assert event["outcome"] == "refused"
    assert event["waited"] == "0"
    assert event["pid"] == str(os.getpid())
    assert event["rest"] == f' holder_pid={os.getpid()} holder_label="peer run: pytest -n auto"'


def test_a_wait_that_runs_out_logs_one_gave_up_line_with_the_wait(
        lease_root: Path, events_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _suite_lease.acquire("holder", lease_root=lease_root)
    assert first is not None
    _fake_clock(monkeypatch)
    try:
        assert _suite_lease.acquire("waiter", wait_seconds=3, lease_root=lease_root) is None
    finally:
        first()
    (event,) = _events(events_log)
    assert event["outcome"] == "gave_up"
    assert event["waited"] == "3"
    assert event["rest"] == f" holder_pid={os.getpid()} holder_label=holder"


def test_a_wait_that_succeeds_logs_one_acquired_after_wait_line(
        lease_root: Path, events_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _suite_lease.acquire("holder", lease_root=lease_root)
    assert first is not None
    sleeps: list[float] = []

    def release_on_third_sleep(_state: dict[str, float]) -> None:
        sleeps.append(1.0)
        if len(sleeps) == 3:
            first()

    _fake_clock(monkeypatch, release_on_third_sleep)
    release = _suite_lease.acquire("waiter", wait_seconds=60, lease_root=lease_root)
    assert release is not None
    release()
    (event,) = _events(events_log)
    assert event["outcome"] == "acquired_after_wait"
    assert event["waited"] == "3"
    assert event["rest"] == f" holder_pid={os.getpid()} holder_label=holder"


def test_reclaiming_a_dead_holders_lease_logs_one_stale_reclaimed_line(lease_root: Path, events_log: Path) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    (lease / "pid").write_text(f"{dead.pid}\n")
    (lease / "label").write_text("crashed run\n")
    release = _suite_lease.acquire("taker", lease_root=lease_root)
    assert release is not None
    release()
    (event,) = _events(events_log)
    assert event["outcome"] == "stale_reclaimed"
    assert event["waited"] == "0"
    assert event["rest"] == f' holder_pid={dead.pid} holder_label="crashed run"'


def test_a_pidless_reclaim_logs_stale_reclaimed_without_a_holder_pid(lease_root: Path, events_log: Path) -> None:
    _pidless_lease(lease_root, "empty-dir")
    release = _suite_lease.acquire("taker", lease_root=lease_root, now=_clock(_T0 + _suite_lease.PIDLESS_GRACE_SECONDS + 1))
    assert release is not None
    release()
    (event,) = _events(events_log)
    assert event["outcome"] == "stale_reclaimed"
    assert event["rest"] == ""


def test_the_early_refusal_in_the_conftest_gate_logs_a_refused_line(
        conftest_gate, lease_root: Path, events_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no wait requested the gate exits 75 before it ever calls acquire: it logs the refusal itself."""
    monkeypatch.setattr(_suite_lease, "_lease_root", lambda: lease_root)
    lease = lease_root / _suite_lease.RESOURCE  # by hand: the fixture's acquire always loses
    lease.mkdir(parents=True)
    (lease / "pid").write_text(f"{os.getpid()}\n")
    (lease / "label").write_text("peer-run\n")
    with pytest.raises(pytest.exit.Exception) as err:
        conftest_gate._take_suite_lease()
    assert err.value.returncode == 75
    (event,) = _events(events_log)
    assert event["outcome"] == "refused"
    assert event["rest"] == f" holder_pid={os.getpid()} holder_label=peer-run"


@pytest.mark.parametrize("how", ["parent-is-a-file", "log-is-a-directory"])
def test_an_unwritable_log_changes_nothing(lease_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    if how == "parent-is-a-file":
        (tmp_path / "blocker").write_text("")
        bad = tmp_path / "blocker" / "events.log"
    else:
        bad = tmp_path / "a-dir"
        bad.mkdir()
    monkeypatch.setenv(_suite_lease.EVENTS_LOG_ENV, str(bad))
    first = _suite_lease.acquire("holder", lease_root=lease_root)
    assert first is not None
    try:
        assert _suite_lease.acquire("refused", lease_root=lease_root) is None  # logs a refusal, into the void
    finally:
        first()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lease = lease_root / _suite_lease.RESOURCE
    lease.mkdir()
    (lease / "pid").write_text(f"{dead.pid}\n")
    release = _suite_lease.acquire("taker", lease_root=lease_root)  # a stale reclaim, logged into the void
    assert release is not None
    release()


def test_the_default_log_path_is_under_xdg_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_suite_lease.EVENTS_LOG_ENV, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert _suite_lease.events_log_path() == tmp_path / "xdg" / "nexus" / "lease-events.log"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert _suite_lease.events_log_path() == tmp_path / "home" / ".local" / "state" / "nexus" / "lease-events.log"


def test_concurrent_writers_never_interleave_a_line(events_log: Path) -> None:
    import threading  # noqa: PLC0415 -- this test only

    def write(i: int) -> None:
        _suite_lease.record_event("suite", "refused", 0, holder_pid=i, holder_label=f"label {i}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = _events(events_log)
    assert len(events) == 40
    assert {m["rest"] for m in events} == {f' holder_pid={i} holder_label="label {i}"' for i in range(40)}
