# SPDX-License-Identifier: AGPL-3.0-or-later
"""A console-wide CTRL_BREAK does not reach a process spawned with the flags
``pg_provision._pg_ctl_start_detached`` uses (RDR-224, nexus-f9bgu.54; test
review S2).

The bug: a plain ``nx daemon service stop`` sends CTRL_BREAK to the supervisor's
pid, which behind the venv trampoline is not a process-group root. Windows then
delivers the break to EVERY process on the supervisor's console, whatever group
each is in, so a PostgreSQL that the supervisor had started shut down with it
(published 7.77.1). ``CREATE_NO_WINDOW`` gives pg_ctl (and the postmaster that
inherits its console) a console of its own, and an event sent to another console
cannot reach it. ``tests/db/test_pg_windows_start.py`` pins the flag value; this
file pins the behaviour the flag is for, without PostgreSQL.

The shape. The test process never takes a break itself: everything runs in a
HELPER that owns a console of its own (``CREATE_NEW_CONSOLE``, hidden). A
console-wide Ctrl+Break once killed a whole test run of this project, so the
helper also lists its console's processes with ``GetConsoleProcessList`` and
refuses to send if the test's pid is on it. The helper spawns

* a FLAGGED child with exactly ``PG_CTL_START_CREATIONFLAGS`` (production's
  flags, imported, not retyped),
* a CONTROL child with no flags, on the helper's console, and
* a GROUP-ONLY child (``CREATE_NEW_PROCESS_GROUP`` without ``CREATE_NO_WINDOW``,
  the shape 7.77.1 shipped), recorded in the result but not asserted,

then sends ``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0)``, which goes to every
process on the helper's console. The control dying is what makes the survival of
the flagged child mean something: a send that delivered nothing leaves both alive
and the test fails on the control.

Windows only, and in the rehearsal's Windows test set. The class is a
``--require-passed`` pattern in ``windows-pg-bundle-rehearsal.yml``, so a skip
fails that job instead of passing it having proved nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nexus.db.pg_provision import PG_CTL_START_CREATIONFLAGS

#: Runs as ``python -I <this> <flags> <workdir> <test pid>`` on the base
#: interpreter (a venv ``python.exe`` is a launcher that starts the interpreter
#: as a child, so its pid would not be the process that spawns). Stdlib only.
_HELPER = textwrap.dedent(
    r'''
    import ctypes, json, os, signal, subprocess, sys, threading, time
    from ctypes import wintypes

    flags, workdir, test_pid = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    CHILD = (
        "import sys, time\n"
        "while True:\n"
        "    open(sys.argv[1], 'ab').write(b'x')\n"
        "    time.sleep(0.1)\n"
    )
    GROUP_ONLY = 0x00000200  # CREATE_NEW_PROCESS_GROUP

    kids = {}  # name -> Popen
    result = {"helper_pid": os.getpid()}


    def write_result():
        tmp = os.path.join(workdir, "result.json.tmp")
        with open(tmp, "w") as fh:
            json.dump(result, fh)
        os.replace(tmp, os.path.join(workdir, "result.json"))


    def kill_all():
        for p in kids.values():
            try:
                p.kill()
            except OSError:
                pass


    def watchdog():
        kill_all()
        result.setdefault("error", "helper watchdog fired")
        write_result()
        os._exit(3)


    timer = threading.Timer(100.0, watchdog)
    timer.daemon = True
    timer.start()

    # The break reaches this process too (it is on its own console). A Python
    # handler turns it into a flag; the loops below sleep in short ticks so the
    # handler can run (a CPython SIGBREAK handler does not run in one long wait).
    seen = []
    signal.signal(signal.SIGBREAK, lambda signum, frame: seen.append(time.monotonic()))

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetConsoleProcessList.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
    k32.GetConsoleProcessList.restype = wintypes.DWORD
    k32.GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL


    def console_pids():
        buf = (wintypes.DWORD * 64)()
        n = k32.GetConsoleProcessList(buf, 64)
        return sorted(int(buf[i]) for i in range(min(n, 64)))


    def spawn(name, creationflags):
        hb = os.path.join(workdir, name + ".hb")
        # Stdio shaped like the production spawn: stdin devnull, stdout a file,
        # stderr folded into it.
        with open(os.path.join(workdir, name + ".out"), "ab") as out:
            p = subprocess.Popen(
                [sys.executable, "-I", "-c", CHILD, hb],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        kids[name] = p
        return hb


    def beats(hb):
        try:
            return os.path.getsize(hb)
        except OSError:
            return 0


    try:
        hbs = {
            "flagged": spawn("flagged", flags),
            "control": spawn("control", 0),
            "grouponly": spawn("grouponly", GROUP_ONLY),
        }
        result["pids"] = {name: p.pid for name, p in kids.items()}
        write_result()

        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not all(beats(h) >= 3 for h in hbs.values()):
            time.sleep(0.2)
        result["started"] = {name: beats(h) >= 3 for name, h in hbs.items()}
        if not all(result["started"].values()):
            result["error"] = "a child never wrote a heartbeat"
            write_result()
            sys.exit(0)

        before = console_pids()
        result["console_before"] = before
        if test_pid in before:
            # Never send a break to a console the test process is on.
            result["error"] = "the test process is on the helper's console; nothing was sent"
            write_result()
            sys.exit(0)

        beats_before = {name: beats(h) for name, h in hbs.items()}
        ok = bool(k32.GenerateConsoleCtrlEvent(1, 0))  # CTRL_BREAK_EVENT, every process on this console
        result["send_ok"] = ok
        result["send_error"] = 0 if ok else ctypes.get_last_error()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and kids["control"].poll() is None:
            time.sleep(0.1)
        time.sleep(3.0)  # long enough for a doomed flagged child to have died, and to beat again

        result["helper_saw_break"] = bool(seen)
        result["returncodes"] = {name: p.poll() for name, p in kids.items()}
        result["beats_before"] = beats_before
        result["beats_after"] = {name: beats(h) for name, h in hbs.items()}
        result["console_after"] = console_pids()
        write_result()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the result file is the report
        result["error"] = repr(exc)
        write_result()
    finally:
        kill_all()
        for p in kids.values():
            try:
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
    '''
)


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, 9)  # TerminateProcess on Windows
    except OSError:
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="console control events exist only on Windows")
class TestRealWindows:
    def test_a_console_wide_ctrl_break_kills_an_unflagged_child_and_spares_the_pg_ctl_flagged_one(
        self, tmp_path: Path
    ) -> None:
        workdir = tmp_path / "probe"
        workdir.mkdir()
        script = tmp_path / "console_break_helper.py"
        script.write_text(_HELPER, encoding="utf-8")
        base = getattr(sys, "_base_executable", None) or sys.executable

        startup = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        startup.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined]
        helper_log = tmp_path / "helper.log"
        helper = None
        hung = False
        try:
            with helper_log.open("wb") as log:
                helper = subprocess.Popen(
                    [base, "-I", str(script), str(PG_CTL_START_CREATIONFLAGS), str(workdir), str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    # A console of its own, so the break cannot reach this pytest process.
                    creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
                    startupinfo=startup,
                )
            try:
                helper.wait(timeout=150)
            except subprocess.TimeoutExpired:
                hung = True
                helper.kill()
                pytest.fail(f"the helper did not finish in 150 s: {helper_log.read_text(errors='replace')!r}")
        finally:
            if helper is not None and (hung or helper.poll() is None):
                # The helper hung or the wait was interrupted: it cannot clean up after itself,
                # so end the children it recorded. (A helper that finished killed them already.)
                helper.kill()
                written = workdir / "result.json"
                if written.exists():
                    for pid in json.loads(written.read_text()).get("pids", {}).values():
                        _kill_pid(int(pid))

        result_file = workdir / "result.json"
        assert result_file.exists(), f"the helper wrote no result: rc={helper.returncode} log={helper_log.read_text(errors='replace')!r}"
        r = json.loads(result_file.read_text())
        report = json.dumps(r, sort_keys=True)
        assert "error" not in r, report
        assert r["send_ok"] is True, "GenerateConsoleCtrlEvent failed; nothing was sent: " + report

        rc = r["returncodes"]
        # The control, on the helper's console with no flags: the break killed it. Without this the
        # survival below proves nothing (a send that delivered nothing spares everyone).
        assert rc["control"] is not None, "the control child survived the console-wide break, so it delivered nothing: " + report
        # The child with production's flags: still running, and still beating after the break.
        assert rc["flagged"] is None, "the child spawned with PG_CTL_START_CREATIONFLAGS died of the break: " + report
        assert r["beats_after"]["flagged"] > r["beats_before"]["flagged"], "the flagged child stopped beating: " + report

        # The mechanism: it is not on the console the break went to.
        pids = r["pids"]
        assert pids["control"] in r["console_before"], report
        assert pids["flagged"] not in r["console_before"], "the flagged child shares the helper's console: " + report
