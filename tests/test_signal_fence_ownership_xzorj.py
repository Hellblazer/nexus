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

Then ``fail`` (the default) turns any foreign signal into a failed session, with the signal still sent unchanged;
``report`` and ``off`` are the downgrades. The negative control plants a foreign signal and must go red.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
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
    marker = tmp_path / "argv-marker"
    pid = _detached(tmp_path, [*_SLEEP, str(marker)], _SCRUBBED)
    try:
        assert not fence._has_token(pid, fence._token), "no marker, no ancestry: only rule A can claim it"
        assert _verdict(pid) == ("own", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_rule_a_a_detached_scrubbed_process_whose_environment_names_the_basetemp_is_the_sessions(tmp_path) -> None:
    pid = _detached(tmp_path, _SLEEP, {**_SCRUBBED, "HOME": str(tmp_path / "home")})
    try:
        assert _verdict(pid) == ("own", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_rule_a_matches_a_path_inside_the_basetemp_not_a_sibling_that_shares_its_prefix(tmp_path) -> None:
    sibling = fence._basetemp() + "x/elsewhere"  # pytest-0 versus pytest-0x: a different tree
    pid = _detached(tmp_path, [*_SLEEP, sibling], {**_SCRUBBED, "HOME": sibling})
    try:
        assert _verdict(pid) == ("foreign", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_a_detached_scrubbed_process_naming_nothing_of_the_sessions_is_still_foreign(tmp_path) -> None:
    pid = _detached(tmp_path, _SLEEP, _SCRUBBED)
    try:
        assert _verdict(pid) == ("foreign", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_rule_b_a_reparented_member_of_a_group_whose_live_leader_is_the_sessions(tmp_path) -> None:
    """The leader is this session's child and leads its own group; it starts an intermediate that starts the member
    in the leader's group with a scrubbed environment and exits, so the member is reparented away."""
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
    pid = _detached(tmp_path, _SLEEP, _SCRUBBED)  # leads its own group, naming nothing of ours
    try:
        assert _verdict(pid) == ("foreign", pid)
        assert fence.classify("killpg", pid, signal.SIGTERM, root=_ids()[0], token=_ids()[1],
                              basetemp=fence._basetemp()) == ("foreign", pid)
    finally:
        _reap(pid)


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
def test_negative_control_a_planted_foreign_signal_fails_the_session_by_default(pytester) -> None:
    result = _inner(pytester, _PLANTED, None)
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, "the planted foreign signal must turn the run red"
    result.stdout.fnmatch_lines([
        "signal fence (fail mode, nexus-xzorj): signals=1 own=0 foreign=1 unclassified=0 errors=0",
        "signal fence FAILED: 1 foreign signal(s)*",
    ])


@_linux_fence
def test_negative_control_across_xdist_workers(pytester) -> None:
    result = _inner(pytester, _PLANTED, None, "-n", "2")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED


@_linux_fence
def test_a_session_whose_signals_are_all_its_own_passes(pytester) -> None:
    result = _inner(pytester, _OWN_ONLY, None)
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
    source = _PLANTED + "\ndef test_fails():\n    assert False\n"
    result = _inner(pytester, source, None)
    result.assert_outcomes(passed=1, failed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
