#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a pytest command under a stall watchdog, so a hung or killed run fails loud.

    python scripts/watched_pytest.py [options] -- uv run pytest tests/daemon/ ...

Made for hand runs on remote test hosts (qwentescence, qwent-test, hellmini),
where a run that hangs or dies quietly used to cost half an hour before anyone
looked (nexus-hlvg1: a native-Windows run was killed at 23% by a test's
console-wide Ctrl+Break and nothing said so for 30 minutes).

What it adds to the command:

* ``-v`` and ``-o faulthandler_timeout=<s>``, so the log names each test as it
  starts and a test that runs past the timeout dumps every thread's stack.
* A watchdog on the output. No output for ``--stall`` seconds is a hang: the
  whole process tree is killed, the hung test is named (the last ``-v`` line
  with no result), and the exit is 124.
* Death by a console Ctrl event (Windows ``STATUS_CONTROL_C_EXIT``, POSIX
  SIGINT) is named as that, exit 125, with the test that was running. This
  watcher ignores Ctrl+Break and Ctrl+C itself, so it survives to report it.
* A JSON status file, rewritten every ``--heartbeat`` seconds while the run is
  live and once at the end, that a poller reads in one call instead of
  guessing from a log: ``state`` is running, passed, failed, stalled,
  console_break, timeout or error.
* Optionally a POST of the final status to ``--notify-url``.

Exit codes: the command's own when it finished, 124 stalled, 125 killed by a
console Ctrl event, 126 hit ``--max-total``, 2 bad arguments. Stdlib only.

``-q`` is refused, because quiet output hides which test is running. Under
xdist (``-n``) the hung test cannot be named from the log; the report gives the
last result line instead.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO

#: Windows ``STATUS_CONTROL_C_EXIT`` (0xC000013A), as an unsigned and a signed exit code.
STATUS_CONTROL_C_EXIT: frozenset[int] = frozenset({0xC000013A, 0xC000013A - (1 << 32)})
EXIT_STALLED: int = 124
EXIT_CONSOLE_BREAK: int = 125
EXIT_MAX_TOTAL: int = 126
_RESULT = re.compile(r"\b(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")
_NODEID = re.compile(r"^(?:\[gw\d+\]\s+)?(\S+\.py::\S+)")
_TAIL_LINES: int = 40


@dataclass
class Status:
    state: str
    command: list[str]
    started_at: float
    elapsed_s: float = 0.0
    exit_code: int | None = None
    pid: int | None = None
    last_output_age_s: float = 0.0
    current_test: str | None = None
    last_result_line: str | None = None
    detail: str | None = None
    log: str | None = None
    tail: list[str] = field(default_factory=list)


class _Watch:
    """Tees the child's output to the log and tracks what the run is doing."""

    def __init__(self, log: IO[str]) -> None:
        self._log = log
        self._lock = threading.Lock()
        self.last_output = time.monotonic()
        self.current_test: str | None = None
        self.last_result_line: str | None = None
        self.tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self._partial = ""

    def feed(self, text: str) -> None:
        """Take a chunk of output. A ``-v`` line stays open (no newline) while its
        test runs, so the open tail counts too: that is where a hung test sits."""
        with self._lock:
            self.last_output = time.monotonic()
            self._log.write(text)
            self._log.flush()
            *done, self._partial = (self._partial + text).split("\n")
            for line in done:
                self._see(line.rstrip("\r"), complete=True)
            if self._partial:
                self._see(self._partial.rstrip("\r"), complete=False)

    def _see(self, text: str, *, complete: bool) -> None:
        if complete:
            self.tail.append(text)
        match = _NODEID.match(text)
        if match is None:
            return
        if _RESULT.search(text):
            if complete:
                self.last_result_line = text
            # A -v line that carries its result names a finished test.
            if self.current_test == match.group(1):
                self.current_test = None
        else:
            self.current_test = match.group(1)

    def snapshot(self) -> tuple[float, str | None, str | None, list[str]]:
        with self._lock:
            tail = [*self.tail, self._partial] if self._partial else list(self.tail)
            return (time.monotonic() - self.last_output, self.current_test,
                    self.last_result_line, tail[-_TAIL_LINES:])


def build_command(command: list[str], faulthandler_s: float) -> list[str]:
    """The command with ``-v`` and the faulthandler timeout appended.

    Appended, not inserted: pytest takes options after its paths, and the
    launcher in front of ``pytest`` (``uv run``, a venv path) is left alone.
    """
    if "-q" in command or "--quiet" in command:
        raise ValueError("-q hides which test is running; drop it (this script adds -v)")
    return [*command, "-v", "-o", f"faulthandler_timeout={int(faulthandler_s)}"]


def classify(returncode: int, *, stalled: bool, over_total: bool) -> tuple[str, int]:
    """The final ``state`` and exit code for a run."""
    if stalled:
        return "stalled", EXIT_STALLED
    if over_total:
        return "timeout", EXIT_MAX_TOTAL
    if returncode in STATUS_CONTROL_C_EXIT or returncode == -signal.SIGINT:
        return "console_break", EXIT_CONSOLE_BREAK
    if returncode == 0:
        return "passed", 0
    if returncode in (1, 5):  # tests failed / none collected
        return "failed", returncode
    return "error", returncode


def _spawn(command: list[str], cwd: str | None) -> subprocess.Popen[bytes]:
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(  # noqa: S603 -- the caller's own command, by design
        command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, bufsize=0, **kwargs,  # type: ignore[arg-type]
    )


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill the child and everything it started. Never raises."""
    if proc.poll() is not None and sys.platform == "win32":
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],  # noqa: S603,S607
                           capture_output=True, timeout=30, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _pump(stream: IO[bytes], watch: _Watch) -> None:
    """Read raw chunks, never lines: a hung test's -v line has no newline yet."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while chunk := os.read(stream.fileno(), 65536):
        watch.feed(decoder.decode(chunk))
    if rest := decoder.decode(b"", final=True):
        watch.feed(rest)


def _ignore_console_ctrl() -> None:
    """A test that sends Ctrl+Break to the whole console must not take the watcher with it.

    Windows only. There the child runs in its own process group, so it does not
    inherit this. On POSIX the child would inherit SIG_IGN for SIGINT and its tests
    would behave differently; the child's own session already keeps a terminal
    signal aimed at the watcher's group away from it, and the reverse is the case
    this matters for only on Windows."""
    if sys.platform != "win32":
        return
    for name in ("SIGBREAK", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (OSError, ValueError):
                pass


def _write_status(path: Path | None, status: Status) -> None:
    if path is None:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(status), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _notify(url: str, status: Status) -> str | None:
    body = json.dumps({
        "title": f"watched_pytest {status.state}",
        "message": _summary(status),
        **{k: v for k, v in asdict(status).items() if k != "tail"},
    }).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15):  # noqa: S310 -- caller-chosen URL
            return None
    except OSError as exc:
        return f"notify failed: {exc}"


def _summary(status: Status) -> str:
    where = f" in {status.current_test}" if status.current_test else ""
    return f"{status.state} (exit {status.exit_code}) after {status.elapsed_s:.0f}s{where}"


def run(args: argparse.Namespace, out: IO[str]) -> int:
    try:
        command = build_command(args.command, args.faulthandler_timeout or args.stall)
    except ValueError as exc:
        sys.stderr.write(f"watched_pytest: {exc}\n")
        return 2
    log_path = Path(args.log)
    status_path = Path(args.status) if args.status else None
    _ignore_console_ctrl()
    started = time.time()
    t0 = time.monotonic()
    status = Status(state="running", command=command, started_at=started, log=str(log_path))
    with log_path.open("w", encoding="utf-8") as log:
        watch = _Watch(log)
        proc = _spawn(command, args.cwd)
        status.pid = proc.pid
        assert proc.stdout is not None
        reader = threading.Thread(target=_pump, args=(proc.stdout, watch), daemon=True)
        reader.start()
        stalled = over_total = False
        next_beat = 0.0
        while proc.poll() is None:
            age, current, last_result, tail = watch.snapshot()
            elapsed = time.monotonic() - t0
            if age >= args.stall:
                stalled = True
            elif args.max_total and elapsed >= args.max_total:
                over_total = True
            if stalled or over_total:
                kill_tree(proc)
                break
            if elapsed >= next_beat:
                status.elapsed_s, status.last_output_age_s = elapsed, age
                status.current_test, status.last_result_line, status.tail = current, last_result, tail
                _write_status(status_path, status)
                next_beat = elapsed + args.heartbeat
            time.sleep(min(1.0, args.stall / 4))
        proc.wait()
        reader.join(timeout=10)
    age, current, last_result, tail = watch.snapshot()
    state, code = classify(proc.returncode, stalled=stalled, over_total=over_total)
    status.state, status.exit_code = state, code
    status.elapsed_s, status.last_output_age_s = time.monotonic() - t0, age
    status.current_test, status.last_result_line, status.tail = current, last_result, tail
    if state == "stalled":
        status.detail = f"no output for {args.stall:.0f}s; killed the process tree"
    elif state == "console_break":
        status.detail = (f"the run died to a console Ctrl event (exit {proc.returncode}); "
                         "a test probably sent Ctrl+Break to a pid outside its own process group")
    elif state == "timeout":
        status.detail = f"ran past --max-total {args.max_total:.0f}s; killed the process tree"
    if args.notify_url:
        if (problem := _notify(args.notify_url, status)) is not None:
            status.detail = f"{status.detail or ''}; {problem}".lstrip("; ")
    _write_status(status_path, status)
    print(f"watched_pytest: {_summary(status)}", file=out)
    if state in ("stalled", "console_break", "timeout"):
        print(f"watched_pytest: {status.detail}", file=out)
        for line in tail[-15:]:
            print(f"  | {line}", file=out)
    return code


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="watched_pytest", description=(__doc__ or "").split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--stall", type=float, default=180.0,
                        help="seconds of silence that count as a hang (default 180)")
    parser.add_argument("--faulthandler-timeout", type=float, default=None,
                        help="pytest faulthandler_timeout; default: the --stall value, so a "
                             "hung test dumps its stacks before the kill")
    parser.add_argument("--max-total", type=float, default=0.0,
                        help="hard cap on the whole run in seconds (default: none)")
    parser.add_argument("--heartbeat", type=float, default=30.0,
                        help="seconds between status-file rewrites while running (default 30)")
    parser.add_argument("--log", default="watched-pytest.log", help="output log path")
    parser.add_argument("--status", default="watched-pytest.status.json",
                        help="JSON status file path ('' to disable)")
    parser.add_argument("--notify-url", default=None,
                        help="POST the final status as JSON to this URL")
    parser.add_argument("--cwd", default=None, help="run the command here")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="the pytest command, after --")
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("no command: pass the pytest invocation after --")
    if args.stall <= 0 or args.heartbeat <= 0:
        parser.error("--stall and --heartbeat must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv), sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
