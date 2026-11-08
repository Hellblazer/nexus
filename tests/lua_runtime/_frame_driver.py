# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A frame-level parent for the real worker (RDR-242 Phase 1, nexus-mdbds.5 S1, the worker-path half).

``_runtime_support.run_lua`` drives ``runtime.run`` in process, with the static
check and the frame protocol out of the path. ``run_worker`` here starts the
real ``python -I -m nexus.lua_runtime.worker`` the way the supervisor does,
speaks the frames to it itself, answers its verb calls from a fixture table
and returns the one final frame (a result, an error or a limit) with the calls
it saw, the exit status and the stderr. It is not a second supervisor: no kill
timer on the script's wall time, no Windows job, no ends table. The worker's
own limits (the count hook, ``max_memory``, the result walk) are what it
shows, and a script that gets past them hangs the driver's watchdog, which
kills the child and raises :class:`WorkerHung`, so a test fails and does not
hang.

Launch is shared with production: ``supervisor.worker_argv`` builds the
command (``sys.executable -I -m nexus.lua_runtime.worker`` plus the OS-limit
flags and the reply cap) and ``supervisor.start_worker`` starts it (empty
environment, temp-directory cwd, three pipes, no inherited descriptors).
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from nexus.lua_runtime import frames, oslimits, supervisor
from nexus.lua_runtime.limits import Limits
from nexus.lua_runtime.verb_types import (
    VERB_RESULT_CAP_BYTES,
    VerbArgError,
    VerbContext,
    VerbError,
    VerbSpec,
    VerbTimeout,
    check_args,
    encode_result,
)
from tests.lua_runtime import _support

#: The watchdog: the most a worker may run before the driver kills it. Every test's own wall-time limit is far under it.
DEFAULT_TIMEOUT_S: float = 90.0
#: How long the worker gets to exit after its final frame.
EXIT_WAIT_S: float = 15.0

STDERR_KEEP_BYTES: int = 64 * 1024


class WorkerDriverError(AssertionError):
    """The worker did not follow the protocol, died, or hung: the test fails, it never hangs."""


class WorkerHung(WorkerDriverError):
    """The worker ran past the driver's watchdog and was killed."""


class WorkerDied(WorkerDriverError):
    """The worker ended before its final frame."""


class ProtocolViolation(WorkerDriverError):
    """The worker sent a frame the protocol does not allow at that point."""


@dataclass(frozen=True)
class WorkerRun:
    """What one worker run produced."""

    #: The final frame's tag: ``"result"``, ``"error"`` or ``"limit"``.
    kind: str
    #: The final frame, whole.
    frame: Mapping[str, Any]
    #: Every ``(verb, args)`` the worker called, in order.
    calls: tuple[tuple[str, list[Any]], ...]
    #: The worker's exit status after the final frame (0 is the protocol's).
    exit_code: int
    stderr: str
    pid: int
    argv: tuple[str, ...]

    @property
    def value(self) -> Any:
        assert self.kind == "result", self.frame
        return self.frame["value"]

    @property
    def stats(self) -> Mapping[str, int]:
        return self.frame.get("stats") or {}

    @property
    def error_kind(self) -> str:
        assert self.kind == "error", self.frame
        return str(self.frame["kind"])

    @property
    def detail(self) -> str:
        assert self.kind == "error", self.frame
        return str(self.frame["detail"])

    @property
    def limit(self) -> str:
        assert self.kind == "limit", self.frame
        return str(self.frame["limit"])

    @property
    def limit_value(self) -> Any:
        assert self.kind == "limit", self.frame
        return self.frame["value"]


class _Stderr(threading.Thread):
    """Drains the child's stderr so it can never block on a full pipe; keeps the first 64 KB."""

    def __init__(self, stream: Any) -> None:
        super().__init__(daemon=True, name="frame-driver-stderr")
        self._stream = stream
        self._chunks: list[bytes] = []
        self._kept = 0

    def run(self) -> None:
        try:
            while chunk := self._stream.read(8192):
                room = STDERR_KEEP_BYTES - self._kept
                if room > 0:
                    self._chunks.append(chunk[:room])
                    self._kept += min(len(chunk), room)
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        self.join(timeout=2.0)
        return b"".join(self._chunks).decode("utf-8", "replace")


def start_frame(
    source: str,
    grants: Sequence[str],
    verbs: Mapping[str, VerbSpec],
    args: Mapping[str, Any] | None,
    limits: Limits,
    name: str = "fixture",
) -> dict[str, Any]:
    """The first parent-to-worker frame (T2 nexus/rdr242-interfaces, wire format v6+)."""
    return {
        "t": "start",
        "name": name,
        "source": source,
        "grants": list(grants),
        "verbs": sorted(verbs),
        "args": dict(args or {}),
        "limits": limits.to_frame(),
    }


def _reply(
    call: Mapping[str, Any],
    verbs: Mapping[str, VerbSpec],
    grants: frozenset[str],
    deadline: float,
    ctx: VerbContext,
) -> dict[str, Any]:
    """The parent's answer to one call frame, in the supervisor's order: grant, argument check, the verb, the size."""
    call_id = call["id"]

    def refuse(kind: str, message: str) -> dict[str, Any]:
        return {
            "t": "reply",
            "id": call_id,
            "ok": False,
            "error": {"kind": kind, "message": message},
        }

    name = call["verb"]
    if name not in grants or name not in verbs:
        return refuse("ungranted", f"verb {name} is not granted to this script")
    spec = verbs[name]
    try:
        checked = check_args(spec, call["args"])
    except VerbArgError as exc:
        return refuse("bad_args", str(exc))
    except VerbError as exc:
        return refuse("verb_error", str(exc))
    try:
        value = spec.fn(*checked, deadline=deadline, ctx=ctx)
        encode_result(value)  # the size and JSON check the supervisor makes
    except VerbTimeout as exc:
        raise WorkerDriverError(
            f"{name} raised VerbTimeout: the parent ends the run on wall time, which this driver does not do"
        ) from exc
    except Exception as exc:  # noqa: BLE001 -- a verb error is a reply with ok: false
        return refuse("verb_error", str(exc))
    return {"t": "reply", "id": call_id, "ok": True, "value": value}


def run_worker(
    source: str,
    *grants: str,
    args: Mapping[str, Any] | None = None,
    limits: Limits | None = None,
    verbs: Mapping[str, VerbSpec] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    argv: Sequence[str] | None = None,
    name: str = "fixture",
    on_frame: Callable[[Mapping[str, Any]], None] | None = None,
) -> WorkerRun:
    """Run *source* in a real worker and return its final frame.

    *verbs* is the fixture table (``_support.verb_table``); *grants* are the
    verb names the script is granted, and every verb in the table is in the
    start frame's ``verbs`` list, as the supervisor sends it. *argv* replaces
    the worker command (a stand-in, for the driver's own tests). Raises
    :class:`WorkerHung`, :class:`WorkerDied` or :class:`ProtocolViolation`
    rather than return anything but a final frame from a worker that exited 0.
    """
    lim = limits if limits is not None else _support.limits()
    table = dict(verbs or {})
    granted = frozenset(grants)
    cap = frames.reply_frame_cap(VERB_RESULT_CAP_BYTES)
    command = (
        list(argv)
        if argv is not None
        else supervisor.worker_argv(oslimits.limits_for(lim.wall_time), cap)
    )
    ctx = VerbContext(repo_root=None)
    deadline = time.monotonic() + lim.wall_time
    proc = supervisor.start_worker(command)
    assert proc.stdin is not None and proc.stdout is not None
    stderr = _Stderr(proc.stderr)
    stderr.start()
    hung = threading.Event()

    def kill() -> None:
        hung.set()
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(timeout, kill)
    watchdog.daemon = True
    watchdog.start()
    seen: list[tuple[str, list[Any]]] = []

    def fail(error: type[WorkerDriverError], message: str) -> WorkerDriverError:
        if hung.is_set():
            error, message = (
                WorkerHung,
                f"killed after the {timeout} s watchdog: {message}",
            )
        try:
            proc.kill()
        except OSError:
            pass
        rc = proc.wait()
        return error(f"{message}; worker exit {rc}; stderr: {stderr.text()[-1500:]!r}")

    try:
        try:
            ready = frames.read_frame(proc.stdout, frames.READY_FRAME_CAP)
        except frames.FrameError as exc:
            raise fail(WorkerDied, f"no ready frame ({exc})") from None
        if ready.get("t") != "ready":
            raise fail(
                ProtocolViolation, f"first frame is {ready.get('t')!r}, not ready"
            )
        try:
            frames.write_frame(
                proc.stdin,
                start_frame(source, grants, table, args, lim, name),
                frames.START_FRAME_CAP,
            )
        except OSError as exc:
            raise fail(WorkerDied, f"the start frame cannot be sent ({exc})") from None
        while True:
            try:
                frame = frames.read_frame(proc.stdout, frames.END_FRAME_CAP)
            except frames.FrameError as exc:
                raise fail(WorkerDied, f"no final frame ({exc})") from None
            if on_frame is not None:
                on_frame(frame)
            tag = frame["t"]
            if tag == "call":
                if len(seen) >= frames.MAX_CALLS:
                    raise fail(ProtocolViolation, "more than MAX_CALLS verb calls")
                seen.append((str(frame["verb"]), list(frame["args"])))
                reply = _reply(frame, table, granted, deadline, ctx)
                try:
                    frames.write_frame(proc.stdin, reply, cap)
                except OSError as exc:
                    raise fail(WorkerDied, f"a reply cannot be sent ({exc})") from None
                except frames.FrameError as exc:
                    raise fail(
                        ProtocolViolation, f"the fixture's reply is unsendable ({exc})"
                    ) from None
                continue
            if tag not in ("result", "error", "limit"):
                raise fail(ProtocolViolation, f"unexpected {tag!r} frame")
            break
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            rc = proc.wait(timeout=EXIT_WAIT_S)
        except subprocess.TimeoutExpired:
            raise fail(
                WorkerHung, f"still running {EXIT_WAIT_S} s after its {tag} frame"
            ) from None
        return WorkerRun(
            kind=tag,
            frame=frame,
            calls=tuple(seen),
            exit_code=rc,
            stderr=stderr.text(),
            pid=proc.pid,
            argv=tuple(command),
        )
    finally:
        watchdog.cancel()
        try:
            proc.kill()
        except OSError:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        proc.wait()
