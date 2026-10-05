#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client-side stop driver for the REAL storage-service stack on Windows
(RDR-224 Phase 3, nexus-f9bgu.33 / .34).

The legs of T2 ``nexus_rdr/224-f9bgu17round2``, committed so they are re-run
whenever the stop channel changes (critique S3: the round-2 harness was deleted
from the box and only its output files were kept). It drives the real client,
supervisor, engine and PostgreSQL; ``engine_windows_stop_probe.py`` is the
engine-only sibling.

Topology. The service is started and stopped from a console-less launcher in
the interactive session (session 1) through a one-shot ``/IT`` scheduled task,
which is the shape the logon task has. The refusal leg runs the stop from THIS
shell, which on an ssh login is session 0 and therefore a different session::

    primary   start (session 1); the stop from session 1 exits 0, supervisor and
              engine are gone, the lease is gone, PostgreSQL still accepts
    refused   start (session 1); the stop from this shell exits 1 with REFUSED
              and "Nothing was signalled or killed", and the stack is still up
    withpg    stop --with-pg from session 1; PostgreSQL is down, pg.log shows a
              fast shutdown and a clean one, and the NEXT start logs no crash
              recovery

Each leg ends with a stop of its own (``--with-pg`` in the cleanup), so nothing
this script started is left running; it touches no other process. The verdict is
printed as JSON and the exit code is 0 (every check held), 1 (a check failed) or
2 (the driver could not run: wrong platform, a missing path, or a leg that
could not be exercised, which is a failure and never a skip).

Windows only, stdlib only. Usage::

    python scripts/windows_service_stop_driver.py \\
        --python C:\\build\\x\\venv\\Scripts\\python.exe --config-dir C:\\build\\x\\cfg \\
        --out-dir C:\\build\\x\\out --user sam [--pg-port 5432] [primary refused withpg]

The pure parts (process-table matching, stop-output classification, pg.log
reading, verdict assembly) are tested by tests/test_windows_service_stop_driver.py.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
LEGS: tuple[str, ...] = ("primary", "refused", "withpg")
_CHILD_FLAG = "--run-child"
#: The sentences ``nx daemon service stop`` prints that the legs read.
STOPPED_MARK = "Storage service stopped"
REFUSED_MARK = "REFUSED"
NOTHING_KILLED_MARK = "Nothing was signalled or killed"
PG_STOPPED_MARK = "Postgres stopped"


# --------------------------------------------------------------------------- #
# Pure parts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StackPids:
    """The pids of one config dir's storage-service stack, by role."""

    supervisors: tuple[int, ...] = ()
    engines: tuple[int, ...] = ()
    postgres: tuple[int, ...] = ()

    @property
    def service_up(self) -> bool:
        return bool(self.supervisors or self.engines)

    @property
    def all_pids(self) -> tuple[int, ...]:
        return (*self.supervisors, *self.engines, *self.postgres)


def stack_pids(rows: Sequence[Mapping[str, object]], config_dir: str) -> StackPids:
    """Pick the supervisor, engine and postgres rows of *config_dir* out of a
    ``Win32_Process`` listing (``ProcessId``, ``Name``, ``CommandLine``).

    The match is on the config dir (case-insensitive, Windows paths) so a second
    stack on the box, or an unrelated python, is never counted. The supervisor is
    ``... service start --foreground --config-dir <dir>``; the engine is
    ``nexus-service.exe``; PostgreSQL is ``postgres.exe`` whose command line names
    the data directory under the config dir.
    """
    needle = config_dir.rstrip("\\/").lower()
    sup: list[int] = []
    eng: list[int] = []
    pg: list[int] = []
    for row in rows:
        try:
            pid = int(row["ProcessId"])  # type: ignore[call-overload]
        except (KeyError, TypeError, ValueError):
            continue
        name = str(row.get("Name") or "").lower()
        cmd = str(row.get("CommandLine") or "").lower()
        if needle not in cmd:
            continue
        if name == "nexus-service.exe":
            eng.append(pid)
        elif name == "postgres.exe":
            pg.append(pid)
        elif "service" in cmd and "start" in cmd and "--foreground" in cmd:
            sup.append(pid)
    return StackPids(tuple(sorted(sup)), tuple(sorted(eng)), tuple(sorted(pg)))


@dataclass(frozen=True)
class StopOutput:
    rc: int
    text: str

    @property
    def stopped(self) -> bool:
        return self.rc == 0 and STOPPED_MARK in self.text

    @property
    def refused(self) -> bool:
        return self.rc == 1 and REFUSED_MARK in self.text and NOTHING_KILLED_MARK in self.text

    @property
    def pg_stopped(self) -> bool:
        return PG_STOPPED_MARK in self.text


def analyze_pg_log(text: str) -> dict[str, bool]:
    """What a ``pg.log`` says about how PostgreSQL went down and came back.

    ``fast_shutdown``: a ``--with-pg`` stop reached it. ``clean_shutdown``: it
    wrote its shutdown record. ``crash_recovery``: a start had to REPLAY the log
    (``automatic recovery in progress`` / ``database system was not properly shut
    down``), which a clean stop must never cause. ``stale_pid``: a start was
    refused over a leftover ``postmaster.pid``.
    """
    low = text.lower()
    return {
        "fast_shutdown": "received fast shutdown request" in low,
        "clean_shutdown": "database system is shut down" in low,
        "crash_recovery": (
            "automatic recovery in progress" in low
            or "database system was not properly shut down" in low
            or "database system was interrupted" in low
        ),
        "stale_pid": "lock file \"postmaster.pid\" already exists" in low,
    }


def parse_tasklist_csv(text: str, pid: int) -> bool:
    """True when ``tasklist /FO CSV /NH`` output lists *pid*."""
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[1].strip() == str(pid):
            return True
    return False


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class LegResult:
    leg: str
    checks: list[Check] = field(default_factory=list)
    error: str = ""

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.checks) and all(c.ok for c in self.checks)


def verdict(results: Sequence[LegResult], requested: Sequence[str]) -> tuple[int, dict[str, object]]:
    """The exit code and the JSON body for a run.

    A requested leg with no result, or a result with no checks or an error, is a
    FAILURE: a leg that could not be exercised proved nothing (the vacuous-gate
    doctrine), so it is never reported as a pass.
    """
    by_leg = {r.leg: r for r in results}
    missing = [leg for leg in requested if leg not in by_leg]
    failed = [r.leg for r in results if not r.ok]
    body: dict[str, object] = {
        "requested": list(requested),
        "legs": {
            r.leg: {
                "ok": r.ok,
                "error": r.error,
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in r.checks],
            }
            for r in results
        },
        "missing": missing,
        "failed": failed,
    }
    ok = bool(requested) and not missing and not failed
    body["verdict"] = "PASSED" if ok else "FAILED"
    return (0 if ok else 1), body


def schtasks_create_argv(task: str, user: str, program: str, arguments: str) -> list[str]:
    """argv of the one-shot interactive task that runs *program* in the user's
    interactive session (``/IT``: only while the user is logged on, in session 1)."""
    command = f'"{program}" {arguments}'
    return [
        "schtasks", "/Create", "/TN", task, "/SC", "ONCE", "/ST", "23:59",
        "/RU", user, "/IT", "/F", "/TR", command,
    ]


# --------------------------------------------------------------------------- #
# Windows plumbing
# --------------------------------------------------------------------------- #


def _run(argv: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603


def own_session_id() -> int:
    import ctypes  # noqa: PLC0415 — Windows only

    sid = ctypes.c_ulong()
    ctypes.WinDLL("kernel32").ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    return int(sid.value)


def process_rows() -> list[dict[str, object]]:
    ps = (
        "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine "
        "| ConvertTo-Json -Compress"
    )
    out = _run(["powershell", "-NoProfile", "-Command", ps]).stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def pid_listed(pid: int) -> bool:
    return parse_tasklist_csv(_run(["tasklist", "/FO", "CSV", "/NH", "/FI", f"PID eq {pid}"]).stdout, pid)


def port_accepting(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


@dataclass
class Driver:
    python: str
    config_dir: Path
    out_dir: Path
    user: str
    pg_port: int | None
    stop_timeout_s: float
    pg_log: Path
    sleep: Callable[[float], None] = time.sleep

    def _nx(self, *args: str) -> list[str]:
        return [self.python, "-m", "nexus.cli", *args, "--config-dir", str(self.config_dir)]

    def stack(self) -> StackPids:
        return stack_pids(process_rows(), str(self.config_dir))

    def run_in_session1(self, tag: str, argv: list[str], *, timeout_s: float) -> StopOutput:
        """Run *argv* as the interactive user in session 1 with no console window
        and return its exit code and combined output."""
        result_file = self.out_dir / f"{tag}.result.json"
        result_file.unlink(missing_ok=True)
        pythonw = str(Path(self.python).with_name("pythonw.exe"))
        arguments = subprocess.list2cmdline(
            [str(Path(__file__).resolve()), _CHILD_FLAG, str(result_file), *argv]
        )
        task = f"nx-stop-driver-{tag}"
        created = _run(schtasks_create_argv(task, self.user, pythonw, arguments))
        if created.returncode != 0:
            return StopOutput(-1, f"schtasks /Create failed: {created.stderr.strip()}")
        try:
            _run(["schtasks", "/Run", "/TN", task])
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if result_file.exists():
                    break
                self.sleep(0.5)
            else:
                return StopOutput(-1, f"no result from the session-1 task within {timeout_s} s")
            self.sleep(0.2)
            data = json.loads(result_file.read_text(encoding="utf-8"))
            return StopOutput(int(data["rc"]), str(data["output"]))
        finally:
            _run(["schtasks", "/Delete", "/TN", task, "/F"])

    def wait_for(self, predicate: Callable[[], bool], timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return True
            self.sleep(0.5)
        return predicate()

    def start(self, tag: str) -> StackPids:
        out = self.run_in_session1(f"{tag}-start", self._nx("daemon", "service", "start"), timeout_s=180)
        if out.rc != 0:
            raise RuntimeError(f"service start failed (rc {out.rc}): {out.text.strip()[-400:]}")
        self.wait_for(lambda: self.stack().service_up, 30)
        return self.stack()

    def stop(self, tag: str, *, with_pg: bool = False, session1: bool = True) -> StopOutput:
        args = self._nx("daemon", "service", "stop", *(["--with-pg"] if with_pg else []))
        if session1:
            return self.run_in_session1(f"{tag}-stop", args, timeout_s=self.stop_timeout_s + 30)
        proc = _run(args, timeout=self.stop_timeout_s + 30)
        return StopOutput(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))

    def cleanup(self) -> None:
        if self.stack().service_up or self.stack().postgres:
            self.stop("cleanup", with_pg=True)

    # -- the legs --------------------------------------------------------------

    def leg_primary(self) -> LegResult:
        res = LegResult("primary")
        before = self.start("primary")
        if not res.add("service was up before the stop (non-vacuity)", before.service_up, str(before)):
            return res
        started = time.monotonic()
        out = self.stop("primary")
        took = time.monotonic() - started
        res.add("stop exited 0 and said it stopped", out.stopped, out.text.strip()[-300:])
        res.add("stop returned within the bound", took <= self.stop_timeout_s, f"{took:.1f} s")
        after = self.stack()
        res.add("supervisor and engine are gone", not after.service_up, str(after))
        if self.pg_port is not None:
            res.add("PostgreSQL still accepts connections", port_accepting(self.pg_port), f"port {self.pg_port}")
        return res

    def leg_refused(self) -> LegResult:
        res = LegResult("refused")
        before = self.start("refused")
        if not res.add("service was up before the stop (non-vacuity)", before.service_up, str(before)):
            return res
        here, there = own_session_id(), self._session_of(before)
        if not res.add("this shell is in a different session from the service", here != there, f"own={here} service={there}"):
            return res
        out = self.stop("refused", session1=False)
        res.add("stop exited 1 with REFUSED and Nothing was signalled or killed", out.refused, out.text.strip()[-300:])
        after = self.stack()
        res.add("the stack is still up", after.service_up and after.supervisors == before.supervisors, str(after))
        self.stop("refused-cleanup")
        return res

    def leg_withpg(self) -> LegResult:
        res = LegResult("withpg")
        before = self.start("withpg")
        if not res.add("service was up before the stop (non-vacuity)", before.service_up, str(before)):
            return res
        out = self.stop("withpg", with_pg=True)
        res.add("stop --with-pg exited 0 and stopped PostgreSQL", out.stopped and out.pg_stopped, out.text.strip()[-300:])
        after = self.stack()
        res.add("supervisor, engine and postgres are gone", not after.all_pids, str(after))
        if self.pg_port is not None:
            res.add("PostgreSQL no longer accepts connections", not port_accepting(self.pg_port), f"port {self.pg_port}")
        log = analyze_pg_log(self._read_pg_log())
        res.add("pg.log shows a fast shutdown", log["fast_shutdown"], str(log))
        res.add("pg.log shows a clean shutdown record", log["clean_shutdown"], str(log))
        # The next start must not need crash recovery, and a second stop is clean too.
        self.start("withpg2")
        self.stop("withpg2", with_pg=True)
        log2 = analyze_pg_log(self._read_pg_log())
        res.add("no crash recovery anywhere in pg.log across the second start", not log2["crash_recovery"], str(log2))
        res.add("no stale postmaster.pid refusal", not log2["stale_pid"], str(log2))
        return res

    def _read_pg_log(self) -> str:
        try:
            return self.pg_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _session_of(self, stack: StackPids) -> int:
        import ctypes  # noqa: PLC0415 — Windows only

        sid = ctypes.c_ulong()
        pid = (stack.supervisors or stack.engines)[0]
        ctypes.WinDLL("kernel32").ProcessIdToSessionId(pid, ctypes.byref(sid))
        return int(sid.value)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def _child_main(argv: list[str]) -> int:
    """The session-1 side: run a command with no window and write its result."""
    result_file, cmd = Path(argv[0]), argv[1:]
    proc = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, check=False,
    )
    tmp = result_file.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"rc": proc.returncode, "output": (proc.stdout or "") + (proc.stderr or "")}),
        encoding="utf-8",
    )
    os.replace(tmp, result_file)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == _CHILD_FLAG:
        return _child_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("legs", nargs="*", help=f"any of {', '.join(LEGS)} (default: all)")
    parser.add_argument("--python", required=True, help="the venv python.exe the client runs under")
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--user", required=True, help="the interactive user the /IT task runs as")
    parser.add_argument("--pg-port", type=int, default=None)
    parser.add_argument("--pg-log", type=Path, default=None, help="default: <config-dir>/logs/pg.log")
    parser.add_argument("--stop-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stderr.write("windows_service_stop_driver: Windows only\n")
        return 2
    legs = list(args.legs) or list(LEGS)
    unknown = [leg for leg in legs if leg not in LEGS]
    if unknown:
        sys.stderr.write(f"windows_service_stop_driver: unknown leg(s) {unknown}; choose from {list(LEGS)}\n")
        return 2
    for path in (Path(args.python), args.config_dir):
        if not path.exists():
            sys.stderr.write(f"windows_service_stop_driver: {path} does not exist\n")
            return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    driver = Driver(
        python=args.python, config_dir=args.config_dir, out_dir=args.out_dir, user=args.user,
        pg_port=args.pg_port, stop_timeout_s=args.stop_timeout,
        pg_log=args.pg_log or args.config_dir / "logs" / "pg.log",
    )
    results: list[LegResult] = []
    try:
        for leg in legs:
            try:
                results.append(getattr(driver, f"leg_{leg}")())
            except Exception as exc:  # noqa: BLE001 — a leg that could not run is a failed leg
                results.append(LegResult(leg, error=f"{type(exc).__name__}: {exc}"))
    finally:
        try:
            driver.cleanup()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"windows_service_stop_driver: cleanup failed: {exc}\n")
    code, body = verdict(results, legs)
    text = json.dumps(body, indent=2)
    (args.out_dir / "verdict.json").write_text(text, encoding="utf-8")
    sys.stdout.write(text + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
