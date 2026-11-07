# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-xzorj, alerting step (Sam 2026-11-07, halcyon/xzorj-alerting choice 2): the signal fence's two further
ownership rules, and the check that fails a session on any foreign signal.

The first CI measurement (80577058c) saw 18 foreign signals, all false positives of two classes the diagnostic run
on chas named exactly:

* a process that left the session's process tree (setsid, reparented to init) with a scrubbed environment, but whose
  argv or environment names a path inside this session's basetemp (the nx_setup tests' detached runs carry the test
  box in HOME, NEXUS_CONFIG_DIR and NX_TOOLS_DIR; their nx init and uv children name it in argv): rule A;
* a reparented, scrubbed member of a process group whose live leader is the session's (the macOS guest driver's
  fake ``sleep``): rule B.

Then ``fail`` turns any foreign signal into a failed session, with the signal still sent unchanged; ``report`` (the
default until develop CI with the two rules reads zero foreign) lists only, and ``off`` removes the fence. The negative control plants a foreign signal and must go red.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests import _signal_fence as fence

_linux_fence = pytest.mark.skipif(not fence.active(), reason="the fence reads /proc: Linux only, and not switched off")
_SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
_SCRUBBED = {"PATH": "/usr/bin:/bin"}


def _ids() -> tuple[int, str]:
    return fence._root, fence._token


def _wait_pid(pidfile: Path) -> int:
    deadline = time.monotonic() + 10
    while not pidfile.exists() or not pidfile.read_text():
        assert time.monotonic() < deadline, "the process never wrote its pid"
        time.sleep(0.05)
    return int(pidfile.read_text())


def _detached(tmp_path: Path, argv: list[str], env: dict[str, str]) -> int:
    """A process in its own session, reparented away from this one (its parent exits at once): its pid."""
    pidfile = tmp_path / "detached.pid"
    code = (
        "import subprocess; "
        f"p = subprocess.Popen({argv!r}, start_new_session=True, env={env!r}); "
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=_SCRUBBED)
    return _wait_pid(pidfile)


def _not_pid1_root() -> None:
    """Under a per-job PID namespace pytest can be pid 1, and every orphan then reparents to the root: a test that
    needs a process outside the session cannot build one there."""
    if fence._root <= 1:
        pytest.skip("pytest is pid 1 of its namespace: every orphan reparents to the root")


def _reap(pid: int) -> None:
    try:
        fence._real_kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _verdict(pid: int) -> tuple[str, int]:
    root, token = _ids()
    return fence.classify("kill", pid, signal.SIGTERM, root=root, token=token, basetemp=fence._basetemp())


@_linux_fence
def test_the_session_basetemp_is_the_controllers_root_on_every_worker(tmp_path) -> None:
    bt = Path(fence._basetemp())
    assert bt.is_dir() and tmp_path.resolve().is_relative_to(bt.resolve())
    if os.environ.get("PYTEST_XDIST_WORKER"):
        assert bt.name != os.environ["PYTEST_XDIST_WORKER"] and not bt.name.startswith("popen-gw")


@_linux_fence
def test_rule_a_a_detached_scrubbed_process_whose_argv_names_the_basetemp_is_the_sessions(tmp_path) -> None:
    _not_pid1_root()
    marker = tmp_path / "argv-marker"
    pid = _detached(tmp_path, [*_SLEEP, str(marker)], _SCRUBBED)
    try:
        assert not fence._has_token(pid, fence._token), "no marker, no ancestry: only rule A can claim it"
        assert _verdict(pid) == ("own", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_rule_a_a_detached_scrubbed_process_whose_environment_names_the_basetemp_is_the_sessions(tmp_path) -> None:
    _not_pid1_root()
    pid = _detached(tmp_path, _SLEEP, {**_SCRUBBED, "HOME": str(tmp_path / "home")})
    try:
        assert _verdict(pid) == ("own", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_rule_a_matches_a_path_inside_the_basetemp_not_a_sibling_that_shares_its_prefix(tmp_path) -> None:
    _not_pid1_root()
    sibling = fence._basetemp() + "x/elsewhere"  # pytest-0 versus pytest-0x: a different tree
    pid = _detached(tmp_path, [*_SLEEP, sibling], {**_SCRUBBED, "HOME": sibling})
    try:
        assert _verdict(pid) == ("foreign", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_rule_a_matches_the_basetemp_only_at_a_path_start(tmp_path) -> None:
    """/scratch + the basetemp is another tree that merely ends in the same string."""
    _not_pid1_root()
    pid = _detached(tmp_path, [*_SLEEP, "/scratch" + fence._basetemp() + "/x"], _SCRUBBED)
    try:
        assert _verdict(pid) == ("foreign", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_rule_a_finds_the_basetemp_inside_a_path_list(tmp_path) -> None:
    _not_pid1_root()
    pid = _detached(tmp_path, _SLEEP, {"PATH": f"/usr/bin:{fence._basetemp()}:/bin"})
    try:
        assert _verdict(pid) == ("own", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_a_detached_scrubbed_process_naming_nothing_of_the_sessions_is_still_foreign(tmp_path) -> None:
    _not_pid1_root()
    pid = _detached(tmp_path, _SLEEP, _SCRUBBED)
    try:
        assert _verdict(pid) == ("foreign", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_rule_b_a_reparented_member_of_a_group_whose_live_leader_is_the_sessions(tmp_path) -> None:
    """The leader is this session's child and leads its own group; it starts an intermediate that starts the member
    in the leader's group with a scrubbed environment and exits, so the member is reparented away."""
    _not_pid1_root()
    pidfile = tmp_path / "member.pid"
    member_code = (
        "import os, subprocess; "
        f"p = subprocess.Popen({_SLEEP!r}, env={_SCRUBBED!r}); "
        f"open({str(pidfile)!r}, 'w').write(str(p.pid)); os._exit(0)"
    )
    leader_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {member_code!r}], env={_SCRUBBED!r}).wait(); time.sleep(60)"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_code], start_new_session=True)
    member = _wait_pid(pidfile)
    try:
        st = fence._stat(member)
        assert st is not None and st[1] == leader.pid, "the member is in the leader's group"
        assert st[0] != leader.pid, "and was reparented away from it"
        assert _verdict(member) == ("own", member)
    finally:
        _reap(member)
        leader.kill()
        leader.wait()


@_linux_fence
def test_rule_b_does_not_claim_a_member_of_a_foreign_group(tmp_path) -> None:
    """A live foreign leader (detached, scrubbed, naming nothing of ours) with a reparented member in its group: the
    member is foreign, because rule B asks whether the leader is the session's."""
    _not_pid1_root()
    outside = Path(tempfile.mkdtemp(dir=os.environ.get("NX_TEST_TMP_ORIG") or "/tmp"))
    try:
        if Path(fence._basetemp()) in outside.resolve().parents:
            pytest.skip("no directory outside the basetemp to plant in")
        pidfile = outside / "member.pid"
        member_code = (
            "import os, subprocess; "
            f"p = subprocess.Popen({_SLEEP!r}, env={_SCRUBBED!r}); "
            f"open({str(pidfile)!r}, 'w').write(str(p.pid)); os._exit(0)"
        )
        leader_code = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {member_code!r}], env={_SCRUBBED!r}).wait(); time.sleep(60)"
        )
        leader = _detached(outside, [sys.executable, "-c", leader_code], _SCRUBBED)
        member = _wait_pid(pidfile)
        try:
            st = fence._stat(member)
            assert st is not None and st[1] == leader and st[0] != leader
            assert _verdict(member) == ("foreign", member)
        finally:
            _reap(member)
            _reap(leader)
    finally:
        shutil.rmtree(outside, ignore_errors=True)


@_linux_fence
def test_a_planted_stale_daemon_reaped_at_session_start_is_sanctioned_not_foreign(monkeypatch) -> None:
    """An earlier, dead session left an aspect worker under its own root; the conftest drain (the same function the
    session-start reapers run) signals it through the real os.kill, and the fence counts it sanctioned."""
    _not_pid1_root()
    import tests.conftest as cf  # noqa: PLC0415 — the reaper under test lives there

    stale = Path(tempfile.mkdtemp(dir=os.environ.get("NX_TEST_TMP_ORIG") or "/tmp"))
    try:
        if Path(fence._basetemp()) in stale.resolve().parents:
            pytest.skip("no directory outside the basetemp to plant in")
        (stale / "cfg").mkdir()
        argv = [sys.executable, "-c", "import time; time.sleep(60)", "aspect-worker", "--config-dir", str(stale / "cfg")]
        pid = _detached(stale, argv, _SCRUBBED)
        monkeypatch.setattr(fence, "_counts", {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0})
        assert _verdict(pid) == ("foreign", pid), "outside the reaper's scope it would read foreign"
        try:
            cf._drain_stale_tmp_run(stale)
            deadline = time.monotonic() + 10
            while fence._stat(pid) is not None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert fence._stat(pid) is None or open(f"/proc/{pid}/stat").read().split(") ")[1][0] == "Z", "reaped"
            assert fence._counts.get("sanctioned", 0) >= 1 and fence._counts["foreign"] == 0
        finally:
            _reap(pid)
    finally:
        shutil.rmtree(stale, ignore_errors=True)


# ── the failing check ────────────────────────────────────────────────────────────────────────────────────────

_PLANTED = (
    "import os, signal\n"
    "from tests import _signal_fence as fence\n"
    "def test_plants_a_foreign_signal(monkeypatch):\n"
    "    monkeypatch.setattr(fence, '_real_kill', lambda pid, sig: None)\n"
    "    parent = fence._stat(int(os.environ[fence.ROOT_ENV]))[0]\n"
    "    os.kill(parent, signal.SIGTERM)\n"
)
_OWN_ONLY = (
    "import os, signal, subprocess, sys\n"
    "def test_signals_its_own_child():\n"
    "    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "    os.kill(p.pid, signal.SIGTERM)\n"
    "    p.wait()\n"
)


def _inner(pytester, source: str, mode: str | None, *args: str):
    pytester.makepyfile(test_inner=source)
    with pytest.MonkeyPatch.context() as mp:
        for k in (fence.TOKEN_ENV, fence.ROOT_ENV, fence.MODE_ENV):
            mp.delenv(k, raising=False)
        if mode is not None:
            mp.setenv(fence.MODE_ENV, mode)
        mp.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
        return pytester.runpytest_subprocess("-p", "tests._signal_fence", "-p", "no:cacheprovider", *args)


@_linux_fence
def test_negative_control_a_planted_foreign_signal_fails_the_session_in_fail_mode(pytester) -> None:
    result = _inner(pytester, _PLANTED, "fail")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, "the planted foreign signal must turn the run red"
    result.stdout.fnmatch_lines([
        "signal fence (fail mode, nexus-xzorj): signals=1 own=0 foreign=1 unclassified=0 sanctioned=0 errors=0",
        "signal fence FAILED: 1 foreign signal(s)*",
    ])


@_linux_fence
def test_negative_control_across_xdist_workers(pytester) -> None:
    result = _inner(pytester, _PLANTED, "fail", "-n", "2")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED


@_linux_fence
def test_a_session_whose_signals_are_all_its_own_passes(pytester) -> None:
    result = _inner(pytester, _OWN_ONLY, "fail")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["signal fence (fail mode, nexus-xzorj): signals=1 own=1 foreign=0 *"])


@_linux_fence
def test_report_mode_lists_the_foreign_signal_and_leaves_the_exit_status_alone(pytester) -> None:
    result = _inner(pytester, _PLANTED, "report")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["signal fence (report mode, nexus-xzorj): signals=1 own=0 foreign=1 *"])


@_linux_fence
def test_a_session_that_already_failed_keeps_its_own_exit_status(pytester) -> None:
    """A run that ends with its own nonzero status (here 3, set by pytest.exit) keeps it: fail mode only turns an OK
    into a failure, never rewrites another code to 1."""
    source = _PLANTED + "\ndef test_zz_exits():\n    import pytest\n    pytest.exit('stop', returncode=3)\n"
    result = _inner(pytester, source, "fail")
    assert result.ret == 3


@_linux_fence
def test_a_nested_session_reports_and_leaves_its_parents_verdict_alone(pytester) -> None:
    """A pytest a test spawns inherits the marker and root; its own count must not fail the exit code the parent
    test asserts, so it runs in report mode."""
    # The planted target is the parent of the root it inherits (the outer test's worker's parent): foreign to it.
    pytester.makepyfile(test_inner=_PLANTED)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(fence.MODE_ENV, "fail")
        mp.setenv(fence.ROOT_ENV, str(os.getpid()))  # this session's root: the inner one is nested
        mp.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
        result = pytester.runpytest_subprocess("-p", "tests._signal_fence", "-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.OK, "a nested session's foreign count must not fail the exit code"
    result.stdout.fnmatch_lines(["signal fence (report mode, nexus-xzorj): signals=1 own=0 foreign=1 *"])


@_linux_fence
def test_the_default_is_report_until_the_ci_measurement_reads_zero(pytester) -> None:
    """The flip to fail is one line (DEFAULT_MODE) and this test, after develop CI with rules A and B shows zero
    foreign signals across every shard (nexus-xzorj)."""
    assert fence.DEFAULT_MODE == "report"
    result = _inner(pytester, _PLANTED, None)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["signal fence (report mode, nexus-xzorj): signals=1 own=0 foreign=1 *"])


def test_every_session_start_reaper_opens_the_sanctioned_scope() -> None:
    """The reapers that signal an earlier, dead session's leftovers on purpose: removing the scope from any of them
    would make the fence read their kills foreign (a pool host after an aborted run). Structural, so it runs on
    every platform."""
    import inspect  # noqa: PLC0415

    import tests._engine_substrate as es  # noqa: PLC0415
    import tests.conftest as cf  # noqa: PLC0415

    for fn in (es.sweep_stale_substrate_clusters, es.sweep_integration_owner_records):
        assert getattr(fn, "__wrapped__", None) is not None or "sanctioned" in inspect.getsource(fn), fn.__name__
        src = inspect.getsource(inspect.getmodule(fn))
        assert f'@_signal_fence.sanctioned(' in src.split(f"def {fn.__name__}(")[0].rsplit("\n\n\n", 1)[-1], fn.__name__
    assert "_signal_fence.sanctioned(" in inspect.getsource(cf._drain_stale_tmp_run)
    assert "_signal_fence.sanctioned(" in inspect.getsource(cf._reap_leaked_test_daemons)


def test_the_basetemp_is_read_never_created() -> None:
    """Reading the basetemp must not make it: getbasetemp() before tests/_tmp_root.py moves TMPDIR would pin the run's
    tmp to the wrong disk."""

    class _Factory:
        _basetemp = None

        def getbasetemp(self):  # pragma: no cover — the fence must never call it
            raise AssertionError("the fence created the basetemp")

    class _Config:
        _tmp_path_factory = _Factory()

    saved = (fence._config, fence._basetemp_cache)
    try:
        fence._config, fence._basetemp_cache = _Config(), None
        assert fence._basetemp() == ""
        assert fence._basetemp_cache is None, "an absent basetemp is read again next time, not cached"
    finally:
        fence._config, fence._basetemp_cache = saved


@_linux_fence
@pytest.mark.parametrize("shape", ["-I{bt}/include", "file://{bt}/x", "({bt}/x)", "[{bt}/x]", "a,{bt}/x"])
def test_rule_a_reads_a_path_in_the_common_argv_shapes(tmp_path, shape) -> None:
    _not_pid1_root()
    pid = _detached(tmp_path, [*_SLEEP, shape.format(bt=fence._basetemp())], _SCRUBBED)
    try:
        assert _verdict(pid) == ("own", pid)
    finally:
        _reap(pid)
