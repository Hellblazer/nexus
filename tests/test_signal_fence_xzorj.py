# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-xzorj item 3: the session signal fence (tests/_signal_fence.py), report mode.

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

pytestmark = pytest.mark.skipif(not fence.active(), reason="the fence reads /proc: Linux only, and not switched off")

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


def test_the_controller_set_the_session_marker_every_process_inherits() -> None:
    root, token = _ids()
    assert root > 1 and len(token) == 32
    assert fence.owned(os.getpid(), root=root, token=token) is True


def test_a_child_is_the_sessions(child) -> None:
    assert fence.classify("kill", child.pid, signal.SIGTERM, root=_ids()[0], token=_ids()[1]) == ("own", child.pid)


def test_a_process_the_session_did_not_start_is_foreign() -> None:
    root, token = _ids()
    assert fence.classify("kill", 1, signal.SIGTERM, root=root, token=token) == ("foreign", 1)
    assert fence.classify("kill", -1, signal.SIGTERM, root=root, token=token) == ("foreign", 0)


def test_a_reparented_child_keeps_its_claim_through_the_inherited_marker(tmp_path) -> None:
    pid = _orphan(tmp_path, env=None)
    try:
        assert fence.classify("kill", pid, signal.SIGTERM, root=_ids()[0], token=_ids()[1]) == ("own", pid)
    finally:
        _reap(pid)


def test_a_reparented_child_with_a_scrubbed_environment_reads_as_foreign(tmp_path) -> None:
    """The false-positive class the bead names (the dry probe flagged it): report mode exists to count these."""
    pid = _orphan(tmp_path, env={"PATH": os.environ.get("PATH", "")})
    try:
        assert fence.classify("kill", pid, signal.SIGTERM, root=_ids()[0], token=_ids()[1]) == ("foreign", pid)
    finally:
        _reap(pid)


def test_a_group_is_judged_by_its_leader_and_a_group_of_ours_is_ours() -> None:
    p = subprocess.Popen(_SLEEP, start_new_session=True)
    try:
        root, token = _ids()
        assert fence.classify("killpg", p.pid, signal.SIGTERM, root=root, token=token) == ("own", p.pid)
        assert fence.classify("kill", -p.pid, signal.SIGTERM, root=root, token=token) == ("own", p.pid)
    finally:
        p.kill()
        p.wait()


def test_a_probe_and_a_gone_target_are_not_classified() -> None:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    root, token = _ids()
    assert fence.classify("kill", os.getpid(), 0, root=root, token=token) == ("unclassified", 0)
    assert fence.classify("kill", p.pid, signal.SIGTERM, root=root, token=token) == ("unclassified", p.pid)


def test_the_wrapper_sends_the_signal_unchanged_and_records_only_foreign_targets(child, monkeypatch) -> None:
    assert os.kill is fence._kill, "the fence is installed in this test process"
    events: list[dict] = []
    monkeypatch.setattr(fence, "_events", events)
    monkeypatch.setattr(fence, "_counts", {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0})
    os.kill(child.pid, signal.SIGTERM)
    assert child.wait(timeout=10) == -signal.SIGTERM, "the signal was delivered"
    assert fence._counts["own"] == 1 and events == []
    with pytest.raises(ProcessLookupError):
        os.kill(2**22 + 1, signal.SIGTERM)  # above pid_max: never a live process (nexus-xzorj convention)
    assert fence._counts["unclassified"] == 1


def test_a_foreign_signal_is_recorded_with_the_test_that_sent_it(monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr(fence, "_events", events)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(fence, "_real_kill", lambda pid, sig: sent.append((pid, sig)))
    os.kill(1, signal.SIGTERM)  # recorded, then handed to the stub: init is never signalled
    assert sent == [(1, signal.SIGTERM)]
    assert len(events) == 1
    e = events[0]
    assert e["test"].endswith("test_a_foreign_signal_is_recorded_with_the_test_that_sent_it")
    assert (e["call"], e["target"], e["signal"], e["pid"]) == ("kill", 1, "SIGTERM", 1)


def test_the_session_summary_counts_and_lists_foreign_signals(pytester) -> None:
    pytester.makepyfile(test_probe=(
        "import os, signal\n"
        "from tests import _signal_fence as fence\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(fence, '_real_kill', lambda pid, sig: None)\n"
        "    os.kill(1, signal.SIGTERM)\n"
    ))
    with pytest.MonkeyPatch.context() as mp:
        for k in (fence.TOKEN_ENV, fence.ROOT_ENV):
            mp.delenv(k, raising=False)
        mp.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
        result = pytester.runpytest_subprocess("-p", "tests._signal_fence", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines([
        "signal fence (report mode, nexus-xzorj): signals=1 own=0 foreign=1 unclassified=0",
        '  foreign: *"test": "test_probe.py::test_x"*',
    ])
