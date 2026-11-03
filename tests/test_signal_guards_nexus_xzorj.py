# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-xzorj: no signal is sent to a group or pid a test or the product only HOLDS A NUMBER for.

nexus-hjw3o fixed the sites a dry probe caught. This pins the primitives the rest of the class goes
through: ``process_group`` refuses a group of 0 or 1 on every path and never resolves a group from a pid
whose handle says it was reaped, ``kill_tree(pgid=)`` signals the recorded group without resolving
anything, and ``tests._child_process.kill_if_identity`` signals a pid from a status or pid file only
while its live command line is still the process a test spawned.

Every signal here is recorded instead of delivered (``os.kill``/``os.killpg`` replaced inside
``nexus.util.process_group`` only), and every fabricated pid is ``NEVER_ALIVE_PID`` (above pid_max: labrun's
per-job PID namespaces make a low literal a real process of the same job)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from nexus.util import process_group as pg
from tests import _child_process as cp
from tests._child_process import NEVER_ALIVE_PID, kill_if_identity, pid_is
from tests._module_seam import setattr_in

posix_only = pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Record every ``os.kill`` / ``os.killpg`` the primitive makes, deliver none."""
    calls: list[tuple[str, int, int]] = []
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
    return calls


# -- _signal_group refuses a group that is never one of ours ------------------------------


@posix_only
@pytest.mark.parametrize("pgid", [1, 0, -1, -4242], ids=["init", "own-by-zero", "every-process", "negative"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["term", "kill"])
def test_the_one_group_signaller_refuses_a_pgid_of_one_or_less(pgid: int, sig: int, sent: list) -> None:
    assert pg._signal_group(pgid, sig) is False
    assert sent == [], f"a signal went to group {pgid}: {sent}"


@posix_only
def test_a_pid_whose_group_resolves_to_init_is_not_signalled(sent: list, monkeypatch: pytest.MonkeyPatch) -> None:
    """``safe_killpg(1)``: pid 1 passes the ``pid <= 0`` guard, ``getpgid(1)`` answers 1, and the group
    signaller used to ``killpg(1)``. Also the shape of a recycled pid whose new owner leads init's group."""
    setattr_in(monkeypatch, pg, "os.getpgid", lambda pid: 1)
    assert pg.safe_killpg(NEVER_ALIVE_PID, signal.SIGTERM) is False
    assert pg.safe_killpg(NEVER_ALIVE_PID, pg.KILL_SIGNAL) is False
    assert sent == [], sent


# -- a handle that says it was reaped is never resolved to a group -------------------------


@posix_only
def test_a_reaped_handles_pid_is_never_resolved_to_a_group(sent: list, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kernel handed the reaped pid to a process leading group ``NEVER_ALIVE_PID``: ``getpgid`` would
    answer with it. A ``Popen`` that has been waited on (``returncode`` set) must not get that far."""
    recycled_group = NEVER_ALIVE_PID
    setattr_in(monkeypatch, pg, "os.getpgid", lambda pid: recycled_group)
    done = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 — fixed argv, test-owned
    assert done.wait(timeout=30) == 0 and isinstance(done.returncode, int)
    assert pg.safe_killpg(done, pg.KILL_SIGNAL) is False
    assert pg.kill_tree(done, None) is False
    assert sent == [], f"a reaped handle's pid was resolved to someone else's group: {sent}"


@posix_only
def test_control_a_live_handle_is_still_resolved_and_signalled(sent: list, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-vacuity: the guard above refuses on ``returncode``, not on everything. A handle with no
    returncode yet (live), a bare pid and a mock with an int pid all still reach the group signaller."""
    setattr_in(monkeypatch, pg, "os.getpgid", lambda pid: NEVER_ALIVE_PID)
    live = SimpleNamespace(pid=NEVER_ALIVE_PID - 1, returncode=None)
    assert pg.safe_killpg(live, signal.SIGTERM) is True
    assert pg.safe_killpg(NEVER_ALIVE_PID - 1, signal.SIGTERM) is True
    assert sent == [("killpg", NEVER_ALIVE_PID, signal.SIGTERM)] * 2, sent


# -- the test helpers' group kills go through those guards, not a raw killpg(getpgid(pid)) ---


@pytest.fixture
def sent_everywhere(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Like ``sent``, and the same recording in the test substrate and the child helper, whose group
    kills used to call ``os.killpg`` themselves. ``getpgid`` answers init's group for any pid, the shape
    of a recycled pid whose owner leads it."""
    from tests import _engine_substrate as sub  # noqa: PLC0415 — deferred: heavy import, only these pins need it

    calls: list[tuple[str, int, int]] = []
    for mod in (pg, sub, cp):
        setattr_in(monkeypatch, mod, "os.killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
        setattr_in(monkeypatch, mod, "os.getpgid", lambda pid: 1)
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    return calls


@posix_only
def test_the_child_helpers_group_signals_refuse_a_group_that_resolves_to_init(sent_everywhere: list) -> None:
    proc = SimpleNamespace(pid=NEVER_ALIVE_PID, returncode=None)
    cp.signal_group(proc, signal.SIGTERM)  # type: ignore[arg-type]
    cp.kill_group(proc)  # type: ignore[arg-type]
    assert sent_everywhere == [], sent_everywhere


@posix_only
def test_the_child_helpers_group_signals_never_resolve_a_reaped_handle(sent_everywhere: list) -> None:
    done = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 — fixed argv, test-owned
    assert done.wait(timeout=30) == 0
    cp.kill_group(done)
    cp.stop_group(done, grace_s=0.1)
    assert sent_everywhere == [], sent_everywhere


@posix_only
def test_the_engine_sweeps_group_kill_refuses_a_group_that_resolves_to_init(sent_everywhere: list) -> None:
    from tests._engine_substrate import _kill_engine_leg  # noqa: PLC0415 — deferred: heavy import

    _kill_engine_leg(NEVER_ALIVE_PID, grace_s=0.1)
    assert sent_everywhere == [], sent_everywhere


# -- kill_tree(pgid=) signals the recorded group and resolves nothing ----------------------


@posix_only
def test_kill_tree_with_a_recorded_group_never_asks_the_pid_for_its_group(sent: list, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[int] = []

    def getpgid(pid: int) -> int:
        asked.append(pid)
        return NEVER_ALIVE_PID - 7  # the recycled owner's group

    setattr_in(monkeypatch, pg, "os.getpgid", getpgid)
    seen: list[tuple[object, int]] = []
    monkeypatch.setattr(pg, "safe_killpg_group", lambda pgid, sig=pg.KILL_SIGNAL: seen.append((pgid, sig)) or True)
    proc = SimpleNamespace(pid=NEVER_ALIVE_PID - 3, returncode=None)  # as an asyncio Process before its watcher reaps
    assert pg.kill_tree(proc, None, pgid=NEVER_ALIVE_PID - 3) is True
    assert asked == [], f"the pid was resolved to a group: {asked}"
    assert seen == [(NEVER_ALIVE_PID - 3, pg.KILL_SIGNAL)], seen
    assert sent == []


# -- a pid from a status or pid file is signalled only while it is still the spawned process --


def _spawn_sleeper(tag: str) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c", f"import time; time.sleep(120)  # {tag}"], start_new_session=True,
    )


def _wait_dead(proc: subprocess.Popen) -> bool:
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return True


@posix_only
def test_kill_if_identity_spares_a_live_process_that_is_not_the_one_spawned() -> None:
    """The recycled-pid case: the number names a LIVE process, but not the sleeper the test started."""
    stranger = _spawn_sleeper("some-other-jobs-process")
    try:
        assert pid_is(stranger.pid, "the-process-this-test-spawned") is False
        assert kill_if_identity(stranger.pid, "the-process-this-test-spawned") is False
        time.sleep(0.2)
        assert stranger.poll() is None, "a live process with another command line was killed"
    finally:
        stranger.kill()
        stranger.wait(timeout=10)


@posix_only
def test_kill_if_identity_kills_the_process_it_matches() -> None:
    """Non-vacuity: the same call with the right identity does kill."""
    mine = _spawn_sleeper("the-process-this-test-spawned")
    try:
        assert pid_is(mine.pid, "the-process-this-test-spawned") is True
        assert kill_if_identity(mine.pid, "the-process-this-test-spawned") is True
        assert _wait_dead(mine)
        assert mine.returncode == -signal.SIGKILL
    finally:
        if mine.poll() is None:
            mine.kill()
            mine.wait(timeout=10)


@pytest.mark.parametrize(
    "pid", [NEVER_ALIVE_PID, 0, 1, -1, True, None, "4321", os.getpid()],
    ids=["never-alive", "zero", "init", "minus-one", "bool", "none", "str", "self"],
)
def test_kill_if_identity_refuses_a_pid_it_cannot_vouch_for(pid: object) -> None:
    assert kill_if_identity(pid, "python") is False


def test_an_empty_identity_is_refused_because_it_matches_everything() -> None:
    with pytest.raises(ValueError):
        kill_if_identity(os.getpid() + 1, "")
