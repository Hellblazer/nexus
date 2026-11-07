# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The test session's signal fence, report mode (nexus-xzorj item 3).

Phases A and B of nexus-xzorj converted every raw kill site the reviewers found: a pid held only as a number is
never signalled. This is the backstop for the sites nobody has found yet. On a shared pool user (chas and qwent
``nxtest``), hand runs share one PID namespace (labrun_559 measured it 2026-11-07, nexus-b323n), so a stale or
fabricated pid can name another session's process, and a ``killpg`` of it can stop another job's whole group.

In every test process ``os.kill`` and ``os.killpg`` are wrapped. A signal is sent unchanged; before it is, its
target is classified:

* **own**: the target, or for a group its leader (else any live member), descends from the session's root pytest
  process, or carries the session's environment marker (``NX_TEST_SIGNAL_FENCE_TOKEN``, which every child
  inherits unless its environment is scrubbed);
* **foreign**: anything else, including ``kill(-1, ...)`` and a group no member of which is ours;
* a probe (signal 0) and a target whose ``/proc/<pid>/stat`` is gone (it exited) are not classified; a target whose
  environment cannot be read (another user's process) and that does not descend from the root is foreign. Under
  ``hidepid=2`` another user's process has no readable ``stat`` and reads as unclassified.

The identity (root pid and marker) is captured once per process at configure, so a test that edits or clears
``os.environ`` changes nothing here. Each foreign event names the test (or the session phase), the call site, the
signal, the target and its program name (argv[0]'s basename, never its arguments); the summary groups repeats.
Expected false positives: a child reparented away from the session whose environment was scrubbed (``env={...}``)
carries no marker and reads as foreign.

Report mode records every foreign signal and the terminal summary (grep ``signal fence (report mode``) counts and
lists them, from every xdist worker. The bead's next step is measurement: a develop CI run counts the false positives (the dry
probe flagged reparented children with scrubbed environments), and only a measured zero would justify refusing.

Linux only: it reads ``/proc`` and spawns nothing (the posix_spawn census stays as it is). macOS has no ``/proc``,
the laptop is not a shared pool user, and Windows has no process groups. ``NX_TEST_SIGNAL_FENCE=off`` turns it off.
"""
from __future__ import annotations

import json
import os
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
_events: list[dict[str, Any]] = []
_counts = {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0}


def active() -> bool:
    """Whether the fence runs in this process: Linux with ``/proc``, and not switched off."""
    return sys.platform.startswith("linux") and Path("/proc/self/stat").exists() and os.environ.get(MODE_ENV) != "off"


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


def owned(pid: int, *, root: int, token: str) -> bool | None:
    """Whether *pid* is this session's: a descendant of *root* or a carrier of *token*. ``None`` when it is gone."""
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
    return _has_token(pid, token)


def _group_members(pgid: int) -> list[int]:
    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            st = _stat(int(entry.name))
            if st is not None and st[1] == pgid:
                out.append(int(entry.name))
    return out


def classify(kind: str, target: int, sig: int, *, root: int, token: str) -> tuple[str, int]:
    """``("own" | "foreign" | "unclassified", pid)``: the verdict, and the pid it was read from (0 when none)."""
    if sig == 0:
        return "unclassified", 0
    if kind == "kill" and target == -1:
        return "foreign", 0
    if kind == "kill" and target > 0:
        verdict = owned(target, root=root, token=token)
        return ("unclassified", target) if verdict is None else ("own" if verdict else "foreign", target)
    pgid = os.getpgrp() if target == 0 else abs(target)
    leader = owned(pgid, root=root, token=token)
    if leader is not None:
        return ("own" if leader else "foreign"), pgid
    members = _group_members(pgid)
    if not members:
        return "unclassified", 0
    for m in members:
        if owned(m, root=root, token=token):
            return "own", m
    return "foreign", members[0]


def _record(kind: str, target: int, sig: int) -> None:
    """Classify and count one signal. Never raises: the fence never changes whether a signal is sent."""
    try:
        verdict, pid = classify(kind, target, sig, root=_root, token=_token)
        _counts["signals"] += 1
        _counts[verdict] += 1
        if verdict == "foreign":
            try:
                name = signal.Signals(sig).name
            except ValueError:
                name = str(sig)
            caller = sys._getframe(2)  # the frame that called os.kill / os.killpg
            site = "/".join(Path(caller.f_code.co_filename).parts[-2:]) + f":{caller.f_lineno}"
            _events.append({"test": _current_test, "call": kind, "target": target, "signal": name, "site": site,
                            "pid": pid, "cmdline": _cmdline(pid) if pid else "", "fence_pid": os.getpid()})
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
    global _root, _token
    if not active():
        return
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


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Any:
    global _current_test
    _current_test = item.nodeid
    try:
        return (yield)
    finally:
        # Between tests and after the last one (session-scoped fixture teardown, sessionfinish, atexit).
        _current_test = "session teardown or between tests"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not active() or os.kill is not _kill:
        return
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_WORKEROUTPUT_KEY] = {"counts": dict(_counts), "events": _events[:200]}


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    payload = (getattr(node, "workeroutput", None) or {}).get(_WORKEROUTPUT_KEY)
    if payload:
        for k, v in payload["counts"].items():
            _counts[k] = _counts.get(k, 0) + v
        _events.extend(payload["events"])


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    if not active() or hasattr(config, "workeroutput"):
        return
    tr = terminalreporter
    tr.write_line(
        "signal fence (report mode, nexus-xzorj): signals={signals} own={own} foreign={foreign} "
        "unclassified={unclassified} errors={errors}".format(**{"errors": 0, **_counts}))
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for e in _events:
        key = (e["test"], e["call"], e["signal"], e.get("site", ""))
        if key in grouped:
            grouped[key]["repeats"] += 1
        else:
            grouped[key] = {**e, "repeats": 1}
    for e in list(grouped.values())[:_MAX_LISTED]:
        tr.write_line("  foreign: " + json.dumps(e, sort_keys=True))
    if len(grouped) > _MAX_LISTED:
        tr.write_line(f"  ... {len(grouped) - _MAX_LISTED} more distinct")


def pytest_unconfigure(config: pytest.Config) -> None:
    if os.kill is _kill:
        uninstall()
