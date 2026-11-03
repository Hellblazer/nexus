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
import uuid
from types import SimpleNamespace

import pytest

from nexus.util import process_group as pg
from tests import _child_process as cp
from tests._child_process import NEVER_ALIVE_PID, group_alive, kill_if_identity, pid_is
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
@pytest.mark.parametrize("sig", [signal.SIGTERM, pg.KILL_SIGNAL], ids=["term", "kill"])
def test_the_one_group_signaller_refuses_a_pgid_of_one_or_less(
    pgid: int, sig: int, sent: list, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pg.KILL_SIGNAL``, not ``signal.SIGKILL``: this runs at collection, where Windows has no ``SIGKILL``.

    A hard signal takes the tracker-sparing path, which reads the process table first and signals the members
    it finds; against group 1 that finds nothing and sends nothing, with or without the pgid guard. The table
    is made unreadable so the hard signal falls back to the plain ``killpg`` that the guard alone stands in
    front of: with the guard deleted the record is not empty for either signal."""
    monkeypatch.setattr(pg, "_group_member_rows", lambda _pgid: None)
    assert pg._signal_group(pgid, sig) is False
    assert sent == [], f"a signal went to group {pgid}: {sent}"


@posix_only
def test_a_pid_whose_group_resolves_to_init_is_not_signalled(sent: list, monkeypatch: pytest.MonkeyPatch) -> None:
    """``safe_killpg(1)``: pid 1 passes the ``pid <= 0`` guard, ``getpgid(1)`` answers 1, and the group
    signaller used to ``killpg(1)``. Also the shape of a recycled pid whose new owner leads init's group."""
    setattr_in(monkeypatch, pg, "os.getpgid", lambda pid: 1)
    monkeypatch.setattr(pg, "_group_member_rows", lambda _pgid: None)
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
    monkeypatch.setattr(pg, "_group_member_rows", lambda _pgid: None)
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


def _record_everywhere(monkeypatch: pytest.MonkeyPatch, group: int) -> list[tuple[str, int, int]]:
    """Record every group kill in the product primitive, the test substrate and the child helper (the last two
    used to call ``os.killpg`` themselves) and deliver none. ``getpgid`` answers *group* for any pid; the table
    is unreadable, so a hard signal that gets past the guards reaches the plain ``killpg`` and is recorded."""
    from tests import _engine_substrate as sub  # noqa: PLC0415 — deferred: heavy import, only these pins need it

    calls: list[tuple[str, int, int]] = []
    for mod in (pg, sub, cp):
        setattr_in(monkeypatch, mod, "os.killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
        setattr_in(monkeypatch, mod, "os.getpgid", lambda pid: group)
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(pg, "_group_member_rows", lambda _pgid: None)
    return calls


@pytest.fixture
def sent_everywhere(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """``getpgid`` answers init's group for any pid, the shape of a recycled pid whose owner leads it."""
    return _record_everywhere(monkeypatch, 1)


@pytest.fixture
def sent_everywhere_recycled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """``getpgid`` answers a group that is NOT init's and not our own (``NEVER_ALIVE_PID``): the pid of a reaped
    handle was recycled to a leader of it. Only the reaped-handle refusal stands between the handle and that
    group, so a pin on it must use this and not :func:`sent_everywhere`, where the pgid <= 1 refusal alone
    already satisfies it."""
    return _record_everywhere(monkeypatch, NEVER_ALIVE_PID)


@posix_only
def test_the_child_helpers_group_signals_refuse_a_group_that_resolves_to_init(sent_everywhere: list) -> None:
    proc = SimpleNamespace(pid=NEVER_ALIVE_PID, returncode=None)
    cp.signal_group(proc, signal.SIGTERM)  # type: ignore[arg-type]
    cp.kill_group(proc)  # type: ignore[arg-type]
    assert sent_everywhere == [], sent_everywhere


@posix_only
def test_the_child_helpers_group_signals_never_resolve_a_reaped_handle(sent_everywhere_recycled: list) -> None:
    done = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 — fixed argv, test-owned
    assert done.wait(timeout=30) == 0
    cp.kill_group(done)
    cp.stop_group(done, grace_s=0.1)
    assert sent_everywhere_recycled == [], sent_everywhere_recycled


@posix_only
def test_the_engine_sweeps_group_kill_refuses_a_group_that_resolves_to_init(sent_everywhere: list) -> None:
    from tests._engine_substrate import _kill_engine_leg  # noqa: PLC0415 — deferred: heavy import

    _kill_engine_leg(NEVER_ALIVE_PID, grace_s=0.1)
    assert sent_everywhere == [], sent_everywhere


@posix_only
def test_the_supervisors_engine_backstop_passes_the_handle_so_a_reaped_engine_is_not_resolved(
    sent: list, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``StorageServiceSupervisor._kill_engine`` used to pass ``proc.pid``, which carries no returncode: an
    engine the supervisor had already reaped was resolved to whoever the kernel gave the pid to. The kill
    goes through the real method; ``getpgid`` answers a group that is not ours."""
    from nexus.daemon.storage_service_daemon import StorageServiceSupervisor  # noqa: PLC0415 — deferred: heavy import

    setattr_in(monkeypatch, pg, "os.getpgid", lambda pid: NEVER_ALIVE_PID)
    monkeypatch.setattr(pg, "_group_member_rows", lambda _pgid: None)
    me = SimpleNamespace(_is_windows=lambda: False, _engine_job=None)
    done = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 — fixed argv, test-owned
    assert done.wait(timeout=30) == 0
    StorageServiceSupervisor._kill_engine(me, done)  # type: ignore[arg-type]
    assert sent == [], f"a reaped engine's pid was resolved to someone else's group: {sent}"
    live = SimpleNamespace(pid=NEVER_ALIVE_PID - 1, returncode=None)  # non-vacuity: a live handle still reaches the group
    StorageServiceSupervisor._kill_engine(me, live)  # type: ignore[arg-type]
    assert sent == [("killpg", NEVER_ALIVE_PID, pg.KILL_SIGNAL)], sent


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


def _nonce() -> str:
    """A per-process token that holds a path separator, as :func:`pid_is` requires, and is unique to one spawn."""
    return f"/nx-xzorj/{uuid.uuid4().hex}"


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
    stranger = _spawn_sleeper(_nonce())
    expected = _nonce()
    try:
        assert pid_is(stranger.pid, expected) is False
        assert kill_if_identity(stranger.pid, expected) is False
        time.sleep(0.2)
        assert stranger.poll() is None, "a live process with another command line was killed"
    finally:
        stranger.kill()
        stranger.wait(timeout=10)


@posix_only
def test_kill_if_identity_kills_the_process_it_matches() -> None:
    """Non-vacuity: the same call with the right identity does kill."""
    tag = _nonce()
    mine = _spawn_sleeper(tag)
    try:
        assert pid_is(mine.pid, tag) is True
        assert kill_if_identity(mine.pid, tag) is True
        assert _wait_dead(mine)
        assert mine.returncode == -signal.SIGKILL
    finally:
        if mine.poll() is None:
            mine.kill()
            mine.wait(timeout=10)


@posix_only
def test_every_token_must_match_so_a_shared_script_path_needs_its_run_nonce() -> None:
    """A script path every run shares is not an identity alone: it needs the per-run token beside it."""
    tag = _nonce()
    mine = _spawn_sleeper(tag)
    try:
        assert pid_is(mine.pid, tag, "time.sleep(120)") is True
        assert pid_is(mine.pid, tag, _nonce()) is False
        assert kill_if_identity(mine.pid, tag, _nonce()) is False
        assert mine.poll() is None
    finally:
        mine.kill()
        mine.wait(timeout=10)


@pytest.mark.parametrize("pid", [NEVER_ALIVE_PID, 0, 1, -1, True, None, "4321", os.getpid()],
                         ids=["never-alive", "zero", "init", "minus-one", "bool", "none", "str", "self"])
def test_kill_if_identity_refuses_a_pid_it_cannot_vouch_for(pid: object) -> None:
    assert kill_if_identity(pid, "/python") is False


@pytest.mark.parametrize(
    "tokens", [(), ("",), ("sleep 30",), ("time.sleep(120)",), ("python", "nx_setup.py"), ("/ok", "")],
    ids=["none", "empty", "sleep-30", "bare-sleep", "no-separator-in-any", "one-empty"],
)
def test_a_token_that_cannot_name_one_process_is_refused(tokens: tuple[str, ...]) -> None:
    """An empty token matches everything; one with no path separator is every such process's command line
    (``sleep 30`` also matches ``sleep 300``). Refused before any pid is looked at."""
    with pytest.raises(ValueError):
        pid_is(os.getpid() + 1, *tokens)
    with pytest.raises(ValueError):
        kill_if_identity(os.getpid() + 1, *tokens)


# -- the lease reader's probe keeps the shell lib's answer ---------------------------------


@posix_only
def test_group_alive_is_a_probe_that_keeps_the_shell_libs_answer_for_group_zero() -> None:
    """``tests/db/_service_fixture.py``'s lease reader mirrors ``kill -0 -- -PGID``, where 0 is the caller's own
    group and reads as held (the failure direction is always "still held"). Refusing pgid <= 1 there changed
    that answer; only a test that SIGNALS a group refuses it, through ``process_group``."""
    assert group_alive(0) is True
    assert group_alive(os.getpgrp()) is True
    assert group_alive(NEVER_ALIVE_PID) is False
