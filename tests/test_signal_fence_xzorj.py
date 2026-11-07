# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-xzorj item 3: the session signal fence (tests/_signal_fence.py): classification, delivery and the fan-in.

The classification runs against real processes on Linux (``/proc``): a child is the session's, a process the
session did not start is not, a reparented child keeps its claim through the inherited marker, and a reparented
child whose environment was scrubbed is the false-positive class the bead asks to measure. The wrapper sends every
signal unchanged and records the foreign ones. Off Linux the fence is inert, and these tests skip.
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


def _ids() -> tuple[int, str]:
    return int(os.environ[fence.ROOT_ENV]), os.environ[fence.TOKEN_ENV]


@pytest.fixture
def child():
    p = subprocess.Popen(_SLEEP)
    yield p
    p.kill()
    p.wait()


def _orphan(tmp_path: Path, *, env: dict[str, str] | None) -> int:
    """A grandchild whose parent exits at once, so it is reparented away from this session; its pid."""
    pidfile = tmp_path / "orphan.pid"
    code = (
        "import os, subprocess, sys; "
        f"p = subprocess.Popen({_SLEEP!r}, start_new_session=True, env={env!r}); "
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    deadline = time.monotonic() + 10
    while not pidfile.exists() or not pidfile.read_text():
        assert time.monotonic() < deadline, "the orphan never wrote its pid"
        time.sleep(0.05)
    return int(pidfile.read_text())


def _reap(pid: int) -> None:
    try:
        fence._real_kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _not_pid1_root() -> None:
    """Under a per-job PID namespace pytest can be pid 1, and then every orphan reparents to the root: the tests
    that need a process outside the session skip there."""
    if _ids()[0] <= 1:
        pytest.skip("pytest is pid 1 of its namespace: every orphan reparents to the root")


@_linux_fence
def test_the_controller_set_the_session_marker_every_process_inherits() -> None:
    root, token = _ids()
    assert root >= 1 and len(token) == 32
    assert fence.owned(os.getpid(), root=root, token=token) is True


@_linux_fence
def test_a_child_is_the_sessions(child) -> None:
    assert fence.classify("kill", child.pid, signal.SIGTERM, root=_ids()[0], token=_ids()[1]) == ("own", child.pid)


def _outsider(root: int) -> int:
    """The root pytest process's parent (the shell or runner that started it): not the session's."""
    _not_pid1_root()
    st = fence._stat(root)
    if st is None or st[0] <= 0 or fence._stat(st[0]) is None:
        pytest.skip("the root's parent is not readable here (hidepid, or no parent in this namespace)")
    return st[0]


@_linux_fence
def test_a_process_the_session_did_not_start_is_foreign() -> None:
    root, token = _ids()
    outsider = _outsider(root)
    assert fence.classify("kill", outsider, signal.SIGTERM, root=root, token=token) == ("foreign", outsider)
    assert fence.classify("kill", -1, signal.SIGTERM, root=root, token=token) == ("foreign", 0)


@_linux_fence
def test_the_top_of_the_ancestry_owns_everything_under_it() -> None:
    """Under a per-job PID namespace the root can be pid 1, the walk's last step: it must still be reached. The top
    of this process's own ancestry stands in for it (pid 1 on a host, or the namespace's first process)."""
    top, cur = os.getpid(), os.getpid()
    while (st := fence._stat(cur)) is not None and st[0] > 0:
        top = cur = st[0]
    assert fence.owned(os.getpid(), root=top, token="not-the-token") is True


@_linux_fence
def test_a_foreign_group_and_a_group_with_no_member_left(tmp_path) -> None:
    """A scrubbed, reparented process in its own session leads a group nothing of ours is in: foreign."""
    _not_pid1_root()
    root, token = _ids()
    leader = _orphan(tmp_path, env={"PATH": os.environ.get("PATH", "")})
    try:
        assert fence.classify("killpg", leader, signal.SIGTERM, root=root, token=token) == ("foreign", leader)
    finally:
        _reap(leader)
    assert fence.classify("killpg", 2**22 + 1, signal.SIGTERM, root=root, token=token) == ("unclassified", 0)


@_linux_fence
def test_kill_0_and_killpg_0_name_the_callers_own_group() -> None:
    root, token = _ids()
    expected = fence.classify("killpg", os.getpgrp(), signal.SIGTERM, root=root, token=token)
    assert fence.classify("kill", 0, signal.SIGTERM, root=root, token=token) == expected
    assert fence.classify("killpg", 0, signal.SIGTERM, root=root, token=token) == expected


@_linux_fence
def test_a_group_whose_leader_exited_is_judged_by_a_live_member(tmp_path) -> None:
    pidfile = tmp_path / "member.pid"
    leader = subprocess.Popen([sys.executable, "-c", (
        "import os, subprocess, sys; "
        f"p = subprocess.Popen({_SLEEP!r}); open({str(pidfile)!r}, 'w').write(str(p.pid))")],
        start_new_session=True)
    leader.wait()
    member = int(pidfile.read_text())
    try:
        assert fence._stat(leader.pid) is None, "the leader is gone"
        assert fence.classify("killpg", leader.pid, signal.SIGTERM, root=_ids()[0], token=_ids()[1]) == ("own", member)
    finally:
        _reap(member)


@_linux_fence
def test_a_live_child_with_a_scrubbed_environment_is_the_sessions_by_ancestry() -> None:
    p = subprocess.Popen(_SLEEP, env={"PATH": os.environ.get("PATH", "")})
    try:
        root, token = _ids()
        assert not fence._has_token(p.pid, token), "no marker: only the ancestor walk can claim it"
        assert fence.classify("kill", p.pid, signal.SIGTERM, root=root, token=token) == ("own", p.pid)
    finally:
        p.kill()
        p.wait()


@_linux_fence
def test_a_reparented_child_keeps_its_claim_through_the_inherited_marker(tmp_path) -> None:
    pid = _orphan(tmp_path, env=None)
    try:
        assert fence.classify("kill", pid, signal.SIGTERM, root=_ids()[0], token=_ids()[1]) == ("own", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_a_reparented_child_with_a_scrubbed_environment_reads_as_foreign(tmp_path) -> None:
    """The false-positive class the bead names (the dry probe flagged it): report mode exists to count these."""
    _not_pid1_root()
    pid = _orphan(tmp_path, env={"PATH": os.environ.get("PATH", "")})
    try:
        assert fence.classify("kill", pid, signal.SIGTERM, root=_ids()[0], token=_ids()[1]) == ("foreign", pid)
    finally:
        _reap(pid)


@_linux_fence
def test_a_group_is_judged_by_its_leader_and_a_group_of_ours_is_ours() -> None:
    p = subprocess.Popen(_SLEEP, start_new_session=True)
    try:
        root, token = _ids()
        assert fence.classify("killpg", p.pid, signal.SIGTERM, root=root, token=token) == ("own", p.pid)
        assert fence.classify("kill", -p.pid, signal.SIGTERM, root=root, token=token) == ("own", p.pid)
    finally:
        p.kill()
        p.wait()


@_linux_fence
def test_a_probe_and_a_gone_target_are_not_classified() -> None:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    root, token = _ids()
    assert fence.classify("kill", os.getpid(), 0, root=root, token=token) == ("unclassified", 0)
    assert fence.classify("kill", p.pid, signal.SIGTERM, root=root, token=token) == ("unclassified", p.pid)


@_linux_fence
def test_the_wrapper_sends_the_signal_unchanged_and_records_only_foreign_targets(child, monkeypatch) -> None:
    assert os.kill is fence._kill, "the fence is installed in this test process"
    events: list[dict] = []
    monkeypatch.setattr(fence, "_events", events)
    monkeypatch.setattr(fence, "_seen", {})
    monkeypatch.setattr(fence, "_counts", {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0})
    os.kill(child.pid, signal.SIGTERM)
    assert child.wait(timeout=10) == -signal.SIGTERM, "the signal was delivered"
    assert fence._counts["own"] == 1 and events == []
    with pytest.raises(ProcessLookupError):
        os.kill(2**22 + 1, signal.SIGTERM)  # above pid_max: never a live process (nexus-xzorj convention)
    assert fence._counts["unclassified"] == 1


@_linux_fence
def test_a_foreign_signal_is_recorded_with_the_test_that_sent_it(monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr(fence, "_events", events)
    monkeypatch.setattr(fence, "_seen", {})
    # The session's real counts must not carry this test's deliberate foreign signal (review r2).
    monkeypatch.setattr(fence, "_counts", {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0})
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(fence, "_real_kill", lambda pid, sig: sent.append((pid, sig)))
    outsider = _outsider(_ids()[0])
    os.kill(outsider, signal.SIGTERM)  # recorded, then handed to the stub: the outsider is never signalled
    assert sent == [(outsider, signal.SIGTERM)]
    assert len(events) == 1
    e = events[0]
    assert e["test"].endswith("test_a_foreign_signal_is_recorded_with_the_test_that_sent_it")
    assert (e["call"], e["target"], e["signal"], e["pid"]) == ("kill", outsider, "SIGTERM", outsider)
    assert e["site"].startswith("tests/test_signal_fence_xzorj.py:"), e["site"]


@_linux_fence
def test_a_cleared_environment_does_not_change_what_is_the_sessions(child, monkeypatch) -> None:
    """Tests clear os.environ (patch.dict(..., clear=True)); the identity was captured at configure."""
    monkeypatch.setattr(fence, "_counts", {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0})
    for k in (fence.TOKEN_ENV, fence.ROOT_ENV):
        monkeypatch.delenv(k)
    os.kill(child.pid, signal.SIGTERM)
    child.wait(timeout=10)
    assert fence._counts["own"] == 1 and fence._counts["foreign"] == 0


@_linux_fence
def test_a_classification_that_raises_still_sends_the_signal(monkeypatch) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(fence, "_real_kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(fence, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(fence, "_counts", {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0})
    os.kill(4242, signal.SIGTERM)
    assert sent == [(4242, signal.SIGTERM)] and fence._counts["errors"] == 1


_PROBE = (
    "import os, signal\n"
    "from tests import _signal_fence as fence\n"
    "def test_x(monkeypatch):\n"
    "    monkeypatch.setattr(fence, '_real_kill', lambda pid, sig: None)\n"
    "    os.kill(4242 if fence._stat(4242) is None else 2**22 + 1, signal.SIGTERM)\n"
    "    parent = fence._stat(int(os.environ[fence.ROOT_ENV]))[0]\n"
    "    os.kill(parent, signal.SIGTERM)\n"
)


def _inner(pytester, *args: str):
    pytester.makepyfile(test_probe=_PROBE, test_probe2=_PROBE.replace("test_x", "test_y"))
    with pytest.MonkeyPatch.context() as mp:
        for k in (fence.TOKEN_ENV, fence.ROOT_ENV):
            mp.delenv(k, raising=False)
        mp.setenv(fence.MODE_ENV, "report")  # these probes plant foreign signals on purpose; fail mode has its own file
        mp.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
        return pytester.runpytest_subprocess("-p", "tests._signal_fence", "-p", "no:cacheprovider", *args)


@_linux_fence
def test_without_xdist_the_plugin_loads_and_reports(pytester) -> None:
    """``-p no:xdist`` (the dual-embedder release gate runs that way): the xdist-only hook is optional."""
    result = _inner(pytester, "-p", "no:xdist")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["signal fence (report mode, nexus-xzorj): signals=4 own=0 foreign=2 unclassified=2 sanctioned=0 errors=0"])


@_linux_fence
def test_the_workers_counts_fan_in_to_the_controllers_summary(pytester) -> None:
    result = _inner(pytester, "-n", "2")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines([
        "signal fence (report mode, nexus-xzorj): signals=4 own=0 foreign=2 unclassified=2 sanctioned=0 errors=0",
        '  foreign: *"test": "test_probe*.py::test_*"*',
    ])


@_linux_fence
def test_switched_off_the_fence_installs_nothing_and_reports_nothing(pytester) -> None:
    pytester.makepyfile(test_off=(
        "import os\n"
        "from tests import _signal_fence as fence\n"
        "def test_off():\n"
        "    assert os.kill is fence._real_kill\n"
    ))
    with pytest.MonkeyPatch.context() as mp:
        for k in (fence.TOKEN_ENV, fence.ROOT_ENV):
            mp.delenv(k, raising=False)
        mp.setenv(fence.MODE_ENV, "off")
        mp.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
        result = pytester.runpytest_subprocess("-p", "tests._signal_fence", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert "signal fence (" not in result.stdout.str()


@_linux_fence
def test_the_logged_program_name_never_carries_arguments(child) -> None:
    assert fence._cmdline(child.pid) == Path(sys.executable).name


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the fence is Linux only by design")
def test_on_linux_the_fence_is_live_unless_switched_off() -> None:
    """Non-vacuity for this module's skip: on Linux every test above skips only when the fence is switched off on
    purpose, never because it failed to install."""
    if os.environ.get(fence.MODE_ENV) == "off":
        pytest.skip("switched off on purpose")
    assert fence.active() and os.kill is fence._kill


@_linux_fence
def test_a_repeating_foreign_signal_is_one_event_with_a_repeat_count(monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr(fence, "_events", events)
    monkeypatch.setattr(fence, "_seen", {})
    monkeypatch.setattr(fence, "_counts", {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0})
    monkeypatch.setattr(fence, "_real_kill", lambda pid, sig: None)
    outsider = _outsider(_ids()[0])
    for _ in range(5):
        os.kill(outsider, signal.SIGTERM)
    assert fence._counts["foreign"] == 5
    assert len(events) == 1 and events[0]["repeats"] == 5
