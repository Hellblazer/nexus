# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The test session's signal fence (nexus-xzorj item 3).

Phases A and B of nexus-xzorj converted every raw kill site the reviewers found: a pid held only as a number is
never signalled. This is the backstop for the sites nobody has found yet. On a shared pool user (chas and qwent
``nxtest``), hand runs share one PID namespace (labrun_559 measured it 2026-11-07, nexus-b323n), so a stale or
fabricated pid can name another session's process, and a ``killpg`` of it can stop another job's whole group.

In every test process ``os.kill`` and ``os.killpg`` are wrapped. A signal is always sent unchanged; before it is,
its target is classified:

* **own**: the target, or for a group its leader (else any live member), descends from the session's root pytest
  process; or carries the session's environment marker (``NX_TEST_SIGNAL_FENCE_TOKEN``, which every child inherits
  unless its environment is scrubbed); or (rule A) names a path inside this session's pytest basetemp in its argv or
  environment (a detached test run carries the test's box in HOME and the like); or, for a single process, (rule B)
  belongs to a process group whose live leader is the session's;
* **sanctioned**: a signal sent inside :func:`sanctioned`, the scope the session-start reapers open (the conftest
  stale-daemon reaper and stale-run drain, the stale-substrate sweep): they signal an earlier, dead session's
  leftovers on purpose, after their own identity checks;
* **foreign**: anything else, including ``kill(-1, ...)`` and a group no member of which is ours;
* a probe (signal 0) and a target whose ``/proc/<pid>/stat`` is gone (it exited) are not classified; a target whose
  environment cannot be read (another user's process) is judged by its ancestry and argv alone. Under
  ``hidepid=2`` another user's process has no readable ``stat`` and reads as unclassified.

The identity (root pid, marker, basetemp) is read once per process, so a test that edits or clears ``os.environ``
changes nothing here. Each foreign event names the test (or the session phase), the call site, the signal, the
target and its program name (argv[0]'s basename up to its first space: never the separate arguments, though a
process that rewrote its title can show a title word); the summary groups repeats.

``fail`` mode (the default, Sam's choice 2026-11-07, halcyon/xzorj-alerting): a session that saw any foreign signal
exits failed (a status already nonzero is kept) and its summary says why (grep ``signal fence (``). The first CI
measurement before the rules saw 18 foreign signals of 6162, all false positives: detached test runs and their
children that left the process tree with a scrubbed environment, every one naming the test's box in its argv or
environment (rule A claims all 18 on the diagnostic run; rule B covers a reparented member of a group we lead, which
that run's members happened to name in their environment too). ``report`` lists them and leaves the exit status
alone; ``off`` installs nothing (``NX_TEST_SIGNAL_FENCE``; case-insensitive; any other value means ``fail``). A
nested pytest session (one a test spawns: it inherits the root's marker) runs in ``report``, so the outermost session
alone owns the verdict.

Linux only: it reads ``/proc`` and spawns nothing (the posix_spawn census stays as it is). macOS has no ``/proc``,
the laptop is not a shared pool user, and Windows has no process groups.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import signal
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

TOKEN_ENV = "NX_TEST_SIGNAL_FENCE_TOKEN"
ROOT_ENV = "NX_TEST_SIGNAL_FENCE_ROOT"
MODE_ENV = "NX_TEST_SIGNAL_FENCE"
_WORKEROUTPUT_KEY = "nx_signal_fence"
_MAX_LISTED = 20

_real_kill: Callable[[int, int], None] = os.kill
_real_killpg: Callable[[int, int], None] = getattr(os, "killpg", None)  # type: ignore[assignment]
#: What the process is doing when it signals: a test's node id while its protocol runs, else the session phase.
_current_test = "session setup"
#: This process's session identity, captured once at configure: a test that edits or clears ``os.environ`` must not
#: change what the fence counts as the session's.
_root = 0
_token = ""
_config: pytest.Config | None = None
_basetemp_cache: str | None = None
#: This process's mode, read once at configure (``fail`` / ``report`` / ``off``); a nested pytest session (one a test
#: spawned, which inherits the root's marker) is downgraded to ``report`` so its own count cannot flip the exit code
#: a parent test asserts.
_mode = "fail"
_installed = False
#: Depth of :func:`sanctioned` scopes on this thread.
_sanction = threading.local()
_events: list[dict[str, Any]] = []
#: Distinct foreign events by (test, call, signal, site), deduplicated as they are recorded, so one repeating test
#: cannot fill the list; ``repeats`` counts the rest. The counts are exact whatever the cap.
_seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
_MAX_DISTINCT = 200
#: Standard-library files that only relay a signal (``Popen.kill`` and friends): the reported site is their caller.
_RELAY_FILES = frozenset({"subprocess.py", "_signal_fence.py"})
_counts = {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0}


def _env_mode() -> str:
    """``NX_TEST_SIGNAL_FENCE``: ``fail`` (the default, and what any other value means), ``report`` or ``off``,
    case-insensitive. The signal itself is sent unchanged in every mode."""
    value = os.environ.get(MODE_ENV, "fail").strip().lower()
    return value if value in ("fail", "report", "off") else "fail"


def mode() -> str:
    """This process's mode as configured (see ``_mode``)."""
    return _mode


@contextlib.contextmanager
def sanctioned(reason: str):
    """Signals sent inside this scope on this thread are counted ``sanctioned``, never ``foreign``: the session-start
    reapers (the stale-daemon reaper, the stale-substrate sweep, the stale-run drain) deliberately signal processes a
    dead earlier session left behind, after their own identity checks (a stale root, a dead lock). *reason* names the
    caller in a recorded event."""
    _sanction.depth = getattr(_sanction, "depth", 0) + 1
    _sanction.reason = reason
    try:
        yield
    finally:
        _sanction.depth -= 1


def _basetemp() -> str:
    """The session's pytest basetemp: the controller's on every xdist worker (a worker's own is
    ``<controller's>/popen-gwN``). Empty when there is none to read."""
    global _basetemp_cache
    if _basetemp_cache is None:
        try:
            factory = getattr(_config, "_tmp_path_factory", None)
            # Read, never create: getbasetemp() would make (and cache) the basetemp before tests/_tmp_root.py has
            # moved TMPDIR, so the first signal of a run would pin it to the wrong disk.
            bt = getattr(factory, "_basetemp", None)
            if bt is None:
                return ""  # not made yet: rule A has nothing to match, and the next call reads again
            bt = Path(bt).resolve()
            if os.environ.get("PYTEST_XDIST_WORKER") and bt.name.startswith("popen-gw"):
                bt = bt.parent
            _basetemp_cache = str(bt)
        except Exception:  # noqa: BLE001 — rule A is then off, never an error
            return ""
    return _basetemp_cache


def active() -> bool:
    """Whether the fence runs in this process: Linux with ``/proc``, and not switched off."""
    return sys.platform.startswith("linux") and Path("/proc/self/stat").exists() and mode() != "off"


def _stat(pid: int) -> tuple[int, int] | None:
    """``(ppid, pgrp)`` of *pid* from ``/proc``, or ``None`` when it is gone or unreadable."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    # The command name (field 2) is parenthesised and may hold spaces or parentheses: split after the last ')'.
    fields = raw[raw.rfind(")") + 2:].split()
    try:
        return int(fields[1]), int(fields[2])
    except (IndexError, ValueError):
        return None


def _has_token(pid: int, token: str) -> bool:
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return f"{TOKEN_ENV}={token}".encode() in env.split(b"\0")


def _cmdline(pid: int) -> str:
    """The basename of the target's argv[0] only: the summary lands in CI logs, and a foreign process's arguments can
    carry anything."""
    try:
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0]
    except OSError:
        return ""
    return Path(argv0.decode("utf-8", "replace")).name.split(" ", 1)[0][:60]  # a rewritten title can hold spaces


def _names_basetemp(pid: int, basetemp: str) -> bool:
    """Rule A: whether *pid*'s argv or environment names a path inside *basetemp* (the session's pytest basetemp, a
    tree no other session's process can name). A path boundary, so ``pytest-0x`` is not inside ``pytest-0``."""
    if not basetemp:
        return False
    # Anchored at both ends: the path starts a field or follows '=', ':', a space or a quote, and is followed by '/',
    # ':', a space, a quote or the end; so /scratch/tmp/pytest-of-u/pytest-0 and pytest-0x do not match.
    pattern = re.compile(rb"(?:^|[=: '\"])" + re.escape(basetemp.rstrip("/").encode()) + rb"(?:$|[/: '\"])")
    for name in ("cmdline", "environ"):
        try:
            raw = Path(f"/proc/{pid}/{name}").read_bytes()
        except OSError:
            continue
        for field in raw.split(b"\0"):
            if pattern.search(field):
                return True
    return False


def owned(pid: int, *, root: int, token: str, basetemp: str = "") -> bool | None:
    """Whether *pid* is this session's: a descendant of *root*, a carrier of *token*, or (rule A) a process whose argv
    or environment names a path inside *basetemp*. ``None`` when it is gone."""
    if _stat(pid) is None:
        return None
    seen: set[int] = set()
    cur = pid
    while cur not in seen:
        if cur == root:
            return True
        if cur <= 1:
            break
        seen.add(cur)
        st = _stat(cur)
        if st is None:
            break
        cur = st[0]
    if _has_token(pid, token) or _names_basetemp(pid, basetemp):
        return True
    return None if _stat(pid) is None else False  # gone mid-read: not classified, never foreign


def _group_members(pgid: int) -> list[int]:
    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            st = _stat(int(entry.name))
            if st is not None and st[1] == pgid:
                out.append(int(entry.name))
    return out


def classify(kind: str, target: int, sig: int, *, root: int, token: str, basetemp: str = "") -> tuple[str, int]:
    """``("own" | "foreign" | "unclassified", pid)``: the verdict, and the pid it was read from (0 when none).

    A single process that is not the session's by :func:`owned` is still the session's (rule B) when its process
    group's live leader is."""
    if sig == 0:
        return "unclassified", 0
    if kind == "kill" and target == -1:
        return "foreign", 0
    if kind == "kill" and target > 0:
        verdict = owned(target, root=root, token=token, basetemp=basetemp)
        if verdict is None:
            return "unclassified", target
        if not verdict:
            st = _stat(target)
            if st is not None and st[1] not in (target, 0) and owned(st[1], root=root, token=token, basetemp=basetemp):
                verdict = True
        return ("own" if verdict else "foreign"), target
    pgid = os.getpgrp() if target == 0 else abs(target)
    leader = owned(pgid, root=root, token=token, basetemp=basetemp)
    if leader is not None:
        return ("own" if leader else "foreign"), pgid
    members = _group_members(pgid)
    if not members:
        return "unclassified", 0
    for m in members:
        if owned(m, root=root, token=token, basetemp=basetemp):
            return "own", m
    return "foreign", members[0]


def _record(kind: str, target: int, sig: int) -> None:
    """Classify and count one signal. Never raises: the fence never changes whether a signal is sent."""
    try:
        if getattr(_sanction, "depth", 0) > 0 and sig != 0:
            _counts["signals"] += 1
            _counts["sanctioned"] = _counts.get("sanctioned", 0) + 1
            return
        verdict, pid = classify(kind, target, sig, root=_root, token=_token,
                                basetemp=_basetemp() if sig != 0 else "")
        _counts["signals"] += 1
        _counts[verdict] += 1
        if verdict == "foreign":
            try:
                name = signal.Signals(sig).name
            except ValueError:
                name = str(sig)
            caller = sys._getframe(2)  # the frame that called os.kill / os.killpg ...
            while caller.f_back is not None and Path(caller.f_code.co_filename).name in _RELAY_FILES:
                caller = caller.f_back  # ... or, through Popen.kill/terminate/send_signal, the code that called those
            site = "/".join(Path(caller.f_code.co_filename).parts[-2:]) + f":{caller.f_lineno}"
            key = (_current_test, kind, name, site)
            if key in _seen:
                _seen[key]["repeats"] += 1
            elif len(_events) < _MAX_DISTINCT:
                _seen[key] = {"test": _current_test, "call": kind, "target": target, "signal": name, "site": site,
                              "pid": pid, "cmdline": _cmdline(pid) if pid else "", "fence_pid": os.getpid(),
                              "repeats": 1}
                _events.append(_seen[key])
    except Exception:  # noqa: BLE001
        _counts["errors"] = _counts.get("errors", 0) + 1


def _kill(pid: int, sig: int) -> None:
    _record("kill", pid, sig)
    _real_kill(pid, sig)


def _killpg(pgid: int, sig: int) -> None:
    _record("killpg", pgid, sig)
    _real_killpg(pgid, sig)


def install() -> None:
    """Wrap ``os.kill`` and ``os.killpg`` in this process (idempotent)."""
    if os.kill is not _kill:
        os.kill = _kill  # type: ignore[assignment]
    if _real_killpg is not None and os.killpg is not _killpg:
        os.killpg = _killpg  # type: ignore[assignment]


def uninstall() -> None:
    os.kill = _real_kill  # type: ignore[assignment]
    if _real_killpg is not None:
        os.killpg = _real_killpg  # type: ignore[assignment]


def pytest_configure(config: pytest.Config) -> None:
    global _root, _token, _config, _mode, _installed
    _mode = _env_mode()
    if not active():
        return
    _config = config
    inherited = os.environ.get(ROOT_ENV, "")
    if inherited and inherited != str(os.getpid()) and not hasattr(config, "workerinput") and _mode == "fail":
        _mode = "report"  # a nested session: the outermost one owns the verdict
    if not os.environ.get(TOKEN_ENV):
        # The controller (or a run without xdist): xdist workers and every child inherit both values.
        os.environ[TOKEN_ENV] = uuid.uuid4().hex
        os.environ[ROOT_ENV] = str(os.getpid())
    try:
        _root = int(os.environ.get(ROOT_ENV, "") or 0)
    except ValueError:
        _root = 0
    _token = os.environ.get(TOKEN_ENV, "")
    install()
    _installed = True


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Any:
    global _current_test
    _current_test = item.nodeid
    try:
        return (yield)
    finally:
        # Between tests and after the last one (session-scoped fixture teardown, sessionfinish, atexit).
        _current_test = "session teardown or between tests"


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _installed:
        return
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_WORKEROUTPUT_KEY] = {"counts": dict(_counts), "events": list(_events)}
        return
    # The controller, every worker's counts already fanned in (pytest_testnodedown runs before this hook).
    if mode() == "fail" and _counts.get("foreign", 0) and not session.exitstatus:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    payload = (getattr(node, "workeroutput", None) or {}).get(_WORKEROUTPUT_KEY)
    if payload:
        for k, v in payload["counts"].items():
            _counts[k] = _counts.get(k, 0) + v
        _events.extend(payload["events"])


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    if not _installed or hasattr(config, "workeroutput"):
        return
    tr = terminalreporter
    tr.write_line(
        f"signal fence ({mode()} mode, nexus-xzorj): "
        "signals={signals} own={own} foreign={foreign} unclassified={unclassified} sanctioned={sanctioned} "
        "errors={errors}".format(**{"errors": 0, "sanctioned": 0, **_counts}))
    if mode() == "fail" and _counts.get("foreign", 0):
        tr.write_line(
            f"signal fence FAILED: {_counts['foreign']} foreign signal(s): a signal reached a process this session "
            "did not start (listed below). Fix the kill, or name the target as the session's (tests/AGENTS.md); "
            f"{MODE_ENV}=report downgrades.", red=True)
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for e in _events:  # distinct per process already; workers can repeat one another's
        key = (e["test"], e["call"], e["signal"], e.get("site", ""))
        if key in grouped:
            grouped[key]["repeats"] += e.get("repeats", 1)
        else:
            grouped[key] = dict(e)
    for e in list(grouped.values())[:_MAX_LISTED]:
        tr.write_line("  foreign: " + json.dumps(e, sort_keys=True))
    if len(grouped) > _MAX_LISTED:
        tr.write_line(f"  ... {len(grouped) - _MAX_LISTED} more distinct")


def pytest_unconfigure(config: pytest.Config) -> None:
    if os.kill is _kill:
        uninstall()
