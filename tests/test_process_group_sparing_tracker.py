# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-x3m8e: EVERY hard group kill in nexus spares the multiprocessing
resource tracker, so the group's POSIX named semaphores are reclaimed.

A whole-group ``SIGKILL`` takes the tracker down with the workers it cleans up
after; Python's named semaphores (spawn/forkserver start methods, which is what
MinerU's ``ProcessPoolExecutor`` uses) are then unlinked by nobody, and macOS
has no way to list or free them (``kern.posix.sem.max`` = 10000; 9,940 leaked,
``mp.Lock()`` raised ENOSPC machine-wide on 2026-10-16). The rule lives in the
shared primitive (``util.process_group._signal_group``), so these tests pin the
primitive and each public door to it (``safe_killpg``, ``safe_killpg_group``,
``kill_tree``, ``bounded_subprocess.kill_child_and_descendants``), not one
caller's copy. The leak tests run a REAL spawn-context pool, kill its group,
and ask the kernel by NAME whether each semaphore it made still exists:
bounded (a handful of names), and everything the control run leaks is unlinked
again in a ``finally``. ``tests/test_process_group_killpg_lint.py`` forbids a
raw ``os.killpg`` in ``src/nexus`` outside the primitive.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time

import pytest

from nexus import bounded_subprocess
from nexus.util import process_group as pg
from tests._child_process import kill_group
from tests._module_seam import delattr_in, setattr_in

posix_only = pytest.mark.skipif(
    not hasattr(os, "killpg"), reason="process groups are POSIX",
)

_TRACKER_ARGV = "from multiprocessing.resource_tracker import main;main(9)"
_TRACKER = (4002, f"python -c {_TRACKER_ARGV}")


def _scripted_table(*rounds: list[tuple[int, str]] | None):
    """A ``_rows`` seam that answers one scripted table per call, repeating the
    last one; ``.reads`` counts the calls."""
    state = {"reads": 0}

    def rows(_pgid: int):
        i = min(state["reads"], len(rounds) - 1)
        state["reads"] += 1
        return rounds[i]

    rows.state = state  # type: ignore[attr-defined]
    return rows


# ── unit: the scan-and-kill loop ─────────────────────────────────────────────


@posix_only
def test_the_pass_signals_everyone_but_the_tracker_and_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: sent.append((pid, sig)))
    rows = [
        (4001, "python -m mineru.cli.fast_api"),
        _TRACKER,
        (4003, "python -c from multiprocessing.spawn import spawn_main"),
        (os.getpid(), "pytest"),
    ]
    assert pg._kill_group_sparing_tracker(
        4001, pg.KILL_SIGNAL, _rows=_scripted_table(rows, [_TRACKER]), _pause=lambda _s: None,
    ) is True
    assert sent == [(4001, pg.KILL_SIGNAL), (4003, pg.KILL_SIGNAL)]


@posix_only
def test_pid_zero_in_the_table_is_never_a_member_and_never_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS ``ps`` lists pid 0 (``kernel_task``), and ``os.getpgid(0)`` answers
    with the CALLER's process group, so a naive membership check reads pid 0 as a
    member of the caller's own group and ``os.kill(0, SIGKILL)`` would signal that
    whole group. Both the reader and the kill loop refuse pid <= 1."""
    sent: list[int] = []
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: sent.append(pid))
    setattr_in(monkeypatch, pg, "os.getpgid", lambda pid: 4001)  # every pid "is" in the group
    monkeypatch.setattr(
        "nexus.daemon.service_registry.all_process_rows",
        lambda: [(0, 99, "kernel_task"), (1, 99, "launchd"), (4001, 5, "worker")],
    )
    assert pg._group_member_rows(4001) == [(4001, "worker")]
    assert pg._kill_group_sparing_tracker(
        4001, pg.KILL_SIGNAL,
        _rows=_scripted_table([(0, "kernel_task"), (1, "launchd"), (4001, "worker")], []),
        _running=lambda _p: True, _pause=lambda _s: None,
    ) is True
    assert sent == [4001]


@posix_only
def test_a_member_forked_between_the_scan_and_the_kill_is_caught_by_the_rescan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: sent.append(pid))
    table = _scripted_table([(5001, "worker"), _TRACKER], [(5002, "respawned worker"), _TRACKER], [_TRACKER])
    assert pg._kill_group_sparing_tracker(
        5001, pg.KILL_SIGNAL, _rows=table, _running=lambda _p: True, _pause=lambda _s: None,
    ) is True
    assert sent == [5001, 5002]
    assert table.state["reads"] == 3, "the loop must read until a clean table, not stop after one round"


@posix_only
def test_only_the_tracker_left_is_success_and_signals_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case that used to trigger a second, whole-group kill (and leak): the
    group is as dead as it may be made."""
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: pytest.fail("signalled the tracker"))
    setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: pytest.fail("whole-group kill"))
    assert pg._kill_group_sparing_tracker(
        4002, pg.KILL_SIGNAL, _rows=_scripted_table([_TRACKER]), _pause=lambda _s: None,
    ) is True


@posix_only
def test_an_empty_group_has_nothing_to_deliver(monkeypatch: pytest.MonkeyPatch) -> None:
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: pytest.fail("signalled in an empty group"))
    assert pg._kill_group_sparing_tracker(
        4002, pg.KILL_SIGNAL, _rows=_scripted_table([]), _pause=lambda _s: None,
    ) is False


@posix_only
def test_a_killed_member_that_is_still_listed_as_a_zombie_ends_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: sent.append(pid))
    table = _scripted_table([(6001, "worker")])
    assert pg._kill_group_sparing_tracker(
        6001, pg.KILL_SIGNAL, _rows=table, _running=lambda _p: False, _pause=lambda _s: None,
    ) is True
    assert sent == [6001], "a zombie awaiting its parent's wait() must not be signalled again"
    assert table.state["reads"] == 2


@posix_only
def test_a_group_refilled_faster_than_it_is_emptied_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: None)
    table = _scripted_table([(7001, "immortal")])
    pg._kill_group_sparing_tracker(
        7001, pg.KILL_SIGNAL, _rows=table, _running=lambda _p: True, _pause=lambda _s: None,
    )
    assert table.state["reads"] == pg._SPARE_MAX_ROUNDS


@posix_only
def test_an_unreadable_table_at_the_start_degrades_to_the_whole_group_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``ps``: the tracker cannot be told from a worker, and leaving the group
    alive would be worse than the old behaviour. The one case that can leak."""
    calls: list[tuple[int, int]] = []
    setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: calls.append((pgid, sig)))
    assert pg._kill_group_sparing_tracker(4001, pg.KILL_SIGNAL, _rows=_scripted_table(None)) is True
    assert calls == [(4001, pg.KILL_SIGNAL)]


@posix_only
def test_an_unreadable_table_after_a_kill_round_never_falls_back_to_the_whole_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: None)
    setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: pytest.fail("whole-group kill after a round"))
    assert pg._kill_group_sparing_tracker(
        8001, pg.KILL_SIGNAL, _rows=_scripted_table([(8001, "worker")], None),
        _running=lambda _p: True, _pause=lambda _s: None,
    ) is True


# ── unit: the door every caller uses ─────────────────────────────────────────


@posix_only
def test_a_catchable_signal_goes_to_the_whole_group_without_reading_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tracker ignores SIGTERM by design, so the polite pass needs no sparing."""
    calls: list[tuple[int, int]] = []
    setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: calls.append((pgid, sig)))
    monkeypatch.setattr(pg, "_group_member_rows", lambda _p: pytest.fail("read the table for SIGTERM"))
    assert pg.safe_killpg_group(4001, signal.SIGTERM) is True
    assert calls == [(4001, signal.SIGTERM)]


@posix_only
def test_the_hard_signal_spares_the_tracker_through_every_public_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: sent.append(pid))
    setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: pytest.fail("whole-group SIGKILL"))
    setattr_in(monkeypatch, pg, "os.getpgid", lambda pid: 4001)
    monkeypatch.setattr(pg, "_group_member_rows", lambda _p: [(4001, "worker"), _TRACKER])
    monkeypatch.setattr(pg, "_pid_running", lambda _p: False)
    assert pg.safe_killpg_group(4001) is True
    assert pg.safe_killpg(4001) is True
    proc = type("P", (), {"pid": 4001})()
    assert pg.kill_tree(proc, None) is True
    assert 4002 not in sent and sent.count(4001) >= 3


@posix_only
@pytest.mark.parametrize("bad", [0, 1, -5, True, "7", None])
def test_a_nonsense_pgid_signals_nothing_and_reads_nothing(bad: object, monkeypatch: pytest.MonkeyPatch) -> None:
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: pytest.fail("signalled on a bad pgid"))
    setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: pytest.fail("signalled on a bad pgid"))
    monkeypatch.setattr(pg, "_group_member_rows", lambda _p: pytest.fail("read the table"))
    assert pg.safe_killpg_group(bad) is False


@posix_only
def test_a_hard_kill_inside_a_sparing_kill_is_a_plain_group_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The table read runs ``ps`` through ``run_bounded``; a hung ``ps`` is
    hard-killed by that same call's timeout handler, which comes back here. It
    must not read the table again (infinite recursion) — the ``ps`` child has no
    tracker — and the guard must be down again once the outer kill returns."""
    calls: list[tuple[int, int]] = []
    setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: calls.append((pgid, sig)))
    reads: list[int] = []

    def table(pgid: int):
        reads.append(pgid)
        assert pg.safe_killpg_group(9001) is True  # the nested kill, as the ps timeout handler makes it
        return []

    monkeypatch.setattr(pg, "_group_member_rows", table)
    assert pg.safe_killpg_group(4001) is False
    assert reads == [4001], "the nested hard kill read the table again"
    assert calls == [(9001, pg.KILL_SIGNAL)]
    assert not getattr(pg._in_sparing_kill, "active", False)


def test_without_process_groups_the_single_process_is_terminated_and_a_group_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows shape: ``os.killpg`` does not exist. Nothing here reads a process
    table or sends a group signal; the one process is killed, as before."""
    sent: list[tuple[int, int]] = []
    delattr_in(monkeypatch, pg, "os.killpg", raising=False)
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(pg, "_group_member_rows", lambda _p: pytest.fail("read the table"))
    assert pg.safe_killpg(4001) is True
    assert sent == [(4001, pg.KILL_SIGNAL)]
    assert pg.safe_killpg_group(4001) is False


# ── real children: a tracker-marked process survives, the others die ─────────


def _spawn_group(marker_in_argv: str | None) -> tuple[subprocess.Popen, int, int]:
    """A group leader with two children in its group: one whose argv carries
    *marker_in_argv* (None: a plain sleeper) and one plain sleeper. Returns
    (leader, tracker_like_pid, plain_pid)."""
    src = (
        "import subprocess, sys, time\n"
        # Built at runtime: the leader's own argv is this source, and a literal
        # marker in it would make the leader itself look like a tracker.
        f"marker = {marker_in_argv!r}.replace('@', '') if {marker_in_argv!r} else None\n"
        "a = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)  # ' + (marker or 'plain')])\n"
        "b = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)  # plain-sibling'])\n"
        "print(a.pid, b.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    leader = subprocess.Popen(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c", src], stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    assert leader.stdout is not None
    a, b = (int(x) for x in leader.stdout.readline().split())
    return leader, a, b


def _gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # a zombie child of the (dead) leader reparented to init is reaped by it;
        # one still parented to this test is ours to reap
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return True
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return False


def _reap_group(leader: subprocess.Popen, *pids: int) -> None:
    for pid in (leader.pid, *pids):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    leader.wait(timeout=10)


def _kill_via_safe_killpg_group(leader: subprocess.Popen) -> bool:
    return pg.safe_killpg_group(leader.pid)


def _kill_via_safe_killpg(leader: subprocess.Popen) -> bool:
    return pg.safe_killpg(leader)


def _kill_via_kill_tree(leader: subprocess.Popen) -> bool:
    return pg.kill_tree(leader, None)


def _kill_via_kill_child_and_descendants(leader: subprocess.Popen) -> bool:
    return bounded_subprocess.kill_child_and_descendants(leader) == "group"


_DOORS = [
    _kill_via_safe_killpg_group,
    _kill_via_safe_killpg,
    _kill_via_kill_tree,
    _kill_via_kill_child_and_descendants,
]


@posix_only
@pytest.mark.parametrize("door", _DOORS, ids=lambda f: f.__name__.removeprefix("_kill_via_"))
def test_a_real_group_loses_everything_but_its_tracker_marked_process(door) -> None:
    leader, tracker_like, plain = _spawn_group("multiprocessing@.resource_tracker")
    try:
        assert door(leader) is True
        leader.wait(timeout=10)
        assert _gone(plain), "an ordinary group member must be killed"
        os.kill(tracker_like, 0)  # still alive: the primitive spared it
    finally:
        _reap_group(leader, tracker_like, plain)


@posix_only
def test_control_a_raw_group_kill_takes_the_tracker_marked_process_too() -> None:
    """Non-vacuity of the test above: a raw ``killpg`` DOES reach it."""
    leader, tracker_like, plain = _spawn_group("multiprocessing@.resource_tracker")
    try:
        kill_group(leader)  # a raw group SIGKILL, the pre-fix behaviour
        leader.wait(timeout=10)
        assert _gone(tracker_like) and _gone(plain)
    finally:
        _reap_group(leader, tracker_like, plain)


# ── the real check: a spawn-context pool, killed, leaks no semaphore ─────────

#: Leader of a spawn-context ``ProcessPoolExecutor``. It records every
#: semaphore the resource tracker is told about, so the test can unlink any the
#: control run leaks. ``abs`` is picklable by reference from a ``-c`` main.
_POOL_LEADER = r"""
import multiprocessing as mp, time
from multiprocessing import resource_tracker as rt
from concurrent.futures import ProcessPoolExecutor

names = []
_orig = rt.register
def _register(name, rtype):
    if rtype == "semaphore":
        names.append(name)
    return _orig(name, rtype)
rt.register = _register

if __name__ == "__main__":
    ctx = mp.get_context("spawn")
    ex = ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    assert ex.submit(abs, -2).result(timeout=90) == 2
    print(" ".join(names), flush=True)
    time.sleep(120)
"""


def _sem_api():
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        libc.sem_open  # noqa: B018 — presence probe
    except AttributeError:
        pytest.skip("libc has no sem_open")
    libc.sem_open.restype = ctypes.c_void_p
    libc.sem_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.sem_close.argtypes = [ctypes.c_void_p]
    libc.sem_unlink.argtypes = [ctypes.c_char_p]
    failed = ctypes.c_void_p(-1).value

    def exists(name: str) -> bool:
        handle = libc.sem_open(name.encode(), 0)
        if handle is None or handle == failed:
            return False
        libc.sem_close(handle)
        return True

    return exists, lambda name: libc.sem_unlink(name.encode())


def _run_pool_and_kill(killer, exists, unlink) -> tuple[list[str], list[str]]:
    """Start the pool leader, kill its group with *killer(leader)*, return
    (names, leaked-names). Every leaked name is unlinked before returning."""
    leader = subprocess.Popen(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c", _POOL_LEADER],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
    )
    names: list[str] = []
    try:
        assert leader.stdout is not None
        names = leader.stdout.readline().split()
        assert names, "the pool leader registered no semaphore: the premise is gone"
        assert all(exists(n) for n in names)
        killer(leader)
        leader.wait(timeout=15)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and any(exists(n) for n in names):
            time.sleep(0.1)  # the spared tracker needs a moment to see EOF and unlink
        return names, [n for n in names if exists(n)]
    finally:
        for n in names:
            if exists(n):
                unlink(n)
        for pid in (m[0] for m in (pg._group_member_rows(leader.pid) or [])):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.kill(leader.pid, signal.SIGKILL)
        except OSError:
            pass
        leader.wait(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX named semaphores")
def test_control_a_raw_whole_group_kill_leaks_the_pools_semaphores() -> None:
    exists, unlink = _sem_api()
    names, leaked = _run_pool_and_kill(kill_group, exists, unlink)  # a raw group SIGKILL
    assert leaked, (
        f"control: a whole-group SIGKILL of a spawn pool should leak its semaphores "
        f"(made {len(names)}); if it no longer does, the tests below prove nothing"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX named semaphores")
@pytest.mark.parametrize("door", _DOORS, ids=lambda f: f.__name__.removeprefix("_kill_via_"))
def test_a_kill_through_the_primitive_leaks_no_semaphore(door) -> None:
    exists, unlink = _sem_api()
    names, leaked = _run_pool_and_kill(door, exists, unlink)
    assert len(names) >= 3
    assert leaked == [], f"{len(leaked)} of {len(names)} semaphores leaked past {door.__name__}"


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups")
def test_the_primitive_refuses_to_signal_nx_s_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(pg.os, "killpg", lambda g, s: sent.append((g, s)))
    own = os.getpgrp()
    assert pg._signal_group(own, signal.SIGTERM) is False
    assert pg._signal_group(own, signal.SIGKILL) is False
    assert sent == [], "a signal reached nx's own process group"
