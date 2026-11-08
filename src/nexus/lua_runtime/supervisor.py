# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The parent side of a script run: start the worker, serve its verb calls, map how it ended (RDR-242 Technical Design 6).

``supervise`` starts one worker (``python -I -m nexus.lua_runtime.worker``),
sends it the ``start`` frame, answers each ``call`` frame by running the verb
on a helper thread, and returns the result or raises one of the errors of
``nexus.lua_runtime.errors`` by the table of worker ends. It is the layer
under ``run_script`` (Phase 3); it takes the fields of the start frame, and
the library-file origin rule belongs to that caller.

Wall time. A kill-timer thread starts with the worker and ends it at the
limit, whatever the run loop is doing, so worker start counts. Each verb runs
on its own helper thread and the run loop waits for the thread *or* the
timer, so ``supervise`` returns at the limit even inside a verb that ignores
its deadline (gate r3 S2); the thread is abandoned and its result dropped.
A verb that raises ``VerbTimeout`` ends the run on wall time at once; the
worker never sees a timeout reply.

Concurrency. One client process runs at most ``frames.MAX_WORKERS`` (4)
workers; a fifth ``supervise`` raises ``ScriptBusyError`` at once.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from nexus.lua_runtime import frames, oslimits
from nexus.lua_runtime.errors import (
    KINDS,
    LIMITS,
    ScriptBusyError,
    ScriptError,
    ScriptLimitError,
    ScriptRunError,
    ScriptWorkerError,
    Stats,
)
from nexus.lua_runtime.limits import DEFAULT_LIMITS, Limits
from nexus.util.nx_argv import argv_off_cwd
from nexus.lua_runtime import verb_types
from nexus.lua_runtime.verb_types import (
    JSONValue,
    VerbArgError,
    VerbContext,
    VerbError,
    VerbSpec,
    VerbTimeout,
    check_args,
    encode_result,
)

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_LIMITS",
    "Limits",
    "RunOutcome",
    "WorkerConfig",
    "supervise",
]

_log = structlog.get_logger(__name__)

MAX_WORKERS: int = frames.MAX_WORKERS
STDERR_KEEP_BYTES: int = 64 * 1024
#: Seconds the worker gets to exit after its final frame before it is killed.
EXIT_GRACE_S: float = 5.0
#: How the worker reports the OS memory limit (a caught ``MemoryError``). On Windows the same status is what the
#: Microsoft C runtime's ``abort()`` returns, so a Lua PANIC abort there is filed as the memory limit too, which is
#: what it is (an allocation the allocator refused); on POSIX it is SIGABRT and a ``ScriptWorkerError``.
EXIT_MEMORY: int = 3

#: How often a run loop waiting on a verb looks at the worker itself (seconds): a worker that died inside a verb
#: that never returns is noticed within this, not at the wall limit.
VERB_POLL_S: float = 0.2

#: Error kinds and limits a worker's final frame may name (the rest are the caller's).
_WORKER_KINDS = frozenset({"static_check", "lua", "result"})
_WORKER_LIMITS = frozenset(LIMITS) - {"cpu_time"}

_slots = threading.BoundedSemaphore(MAX_WORKERS)


@dataclass(frozen=True)
class WorkerConfig:
    """Mechanics a test may change; each default is the RDR's value."""

    #: Replaces the worker command (a test double that speaks the frames).
    argv: tuple[str, ...] | None = None
    cpu_margins: tuple[int, int] = oslimits.DEFAULT_CPU_MARGINS
    os_memory_bytes: int = oslimits.DEFAULT_AS_BYTES
    #: The most the parent sends in one verb result; None is ``verb_types.VERB_RESULT_CAP_BYTES``.
    verb_frame_cap: int | None = None
    max_calls: int = frames.MAX_CALLS
    ctx: VerbContext = field(default_factory=lambda: VerbContext(repo_root=None))
    on_spawn: Callable[[subprocess.Popen[bytes]], None] | None = None


DEFAULT_CONFIG = WorkerConfig()


@dataclass(frozen=True)
class RunOutcome:
    value: JSONValue
    stats: Stats


def _stats(frame: Mapping[str, Any]) -> Stats | None:
    raw = frame.get("stats")
    if not isinstance(raw, dict):
        return None
    return {
        k: v for k, v in raw.items() if isinstance(v, int) and not isinstance(v, bool)
    }


class _StderrKeeper(threading.Thread):
    """Drains the worker's stderr so it can never block on a full pipe; keeps the first 64 KB."""

    def __init__(self, stream: Any) -> None:
        super().__init__(daemon=True, name="lua-worker-stderr")
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

    def head(self) -> str:
        self.join(timeout=2.0)
        return b"".join(self._chunks).decode("utf-8", "replace")


class _Job:
    """The Windows job object: created with kill-on-close, the parent holds its only handle.

    ``close`` is idempotent, because a closed handle number can be reused and
    a second ``CloseHandle`` would close someone else's.
    """

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle
        self._lock = threading.Lock()

    def assign(self, pid: int) -> bool:
        """Put process *pid* in the job. *pid* is what ``Popen`` returned.

        Whether that is the interpreter or a venv launcher that starts the
        interpreter as a child does not change the limits: a child of a job
        member joins the job on creation, and the Windows tests of the worker
        (time limit, memory limit, kill on parent death) pass through the
        job on a uv venv. A launcher that exits at once would leave a worker
        that began outside the job; ``sys.executable`` of a venv is the real
        interpreter on the Windows hosts measured.
        """
        from nexus.util import win_job  # noqa: PLC0415

        with self._lock:
            return self._handle is not None and win_job.assign_process(
                self._handle, pid
            )

    def terminated_by_limit(self) -> bool:
        from nexus.util import win_job  # noqa: PLC0415

        with self._lock:
            return self._handle is not None and win_job.terminated_by_limit(
                self._handle
            )

    def close(self) -> None:
        from nexus.util import win_job  # noqa: PLC0415

        with self._lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            win_job.close_job(handle)


def _kill(proc: subprocess.Popen[bytes], job: _Job | None) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    if job is not None:
        job.close()


class _KillTimer(threading.Thread):
    """Ends the worker at the deadline, independent of the run loop."""

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        job: _Job | None,
        delay: float,
        wake: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="lua-worker-timer")
        self._proc = proc
        self._job = job
        self._delay = delay
        self._wake = wake
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self.fired = False

    def run(self) -> None:
        if self._cancelled.wait(self._delay):
            return
        self.force()

    def force(self) -> None:
        """End the worker now as the wall-time limit (the deadline, or a verb's ``VerbTimeout``)."""
        with self._lock:
            if not self._cancelled.is_set() and self._proc.poll() is None:
                self.fired = True
                _kill(self._proc, self._job)
        # also when the worker is already dead: a run loop waiting on a verb must wake and look at the worker
        self._wake.set()

    def cancel(self) -> bool:
        """Stop the timer; False when it already killed the worker."""
        with self._lock:
            if self.fired:
                return False
            self._cancelled.set()
            return True


class _VerbCall(threading.Thread):
    """One verb call on its own thread, so the run loop can stop waiting for it."""

    def __init__(
        self,
        spec: VerbSpec,
        args: tuple[object, ...],
        deadline: float,
        ctx: VerbContext,
        cap: int,
        wake: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name=f"lua-verb-{spec.name}")
        self._spec = spec
        self._args = args
        self._deadline = deadline
        self._ctx = ctx
        self._cap = cap
        self._wake = wake
        self.finished = False
        self.data: bytes | None = None
        self.timed_out = False
        self.error: str | None = None

    def run(self) -> None:
        # Whatever happens in the verb, the run loop is told: a thread that dies silently leaves the loop waiting
        # for a result that never comes, and the run ends as a false wall_time.
        try:
            self._call()
        except BaseException as exc:  # noqa: BLE001 -- a verb never takes the run loop down, nor leaves it waiting
            self.error = (
                _text(exc) or f"{self._spec.name} failed ({type(exc).__name__})"
            )
        finally:
            self.finished = True
            self._wake.set()

    def _call(self) -> None:
        try:
            value = self._spec.fn(*self._args, deadline=self._deadline, ctx=self._ctx)
            data = encode_result(value)
            if len(data) > self._cap:
                raise VerbError(
                    f"verb result is {len(data)} bytes, over the {self._cap} cap"
                )
            if frames.has_wide_ints(data):
                raise VerbError(
                    f"{self._spec.name}: the result holds an integer outside the 64-bit range"
                )
            self.data = data
        except VerbTimeout:
            self.timed_out = True
        except VerbError as exc:  # includes VerbArgError a verb raises itself
            self.error = _text(exc)
        except Exception as exc:  # noqa: BLE001 -- a verb never takes the run loop down
            try:
                _log.warning(
                    "lua_verb_failed",
                    verb=self._spec.name,
                    exc_class=type(exc).__name__,
                    exc_info=True,
                )
            except Exception:  # noqa: BLE001 -- a log line that cannot be written (a lone surrogate) is not the verb's failure
                pass
            self.error = (
                _text(exc) or f"{self._spec.name} failed ({type(exc).__name__})"
            )


def _text(value: object) -> str:
    """``str(value)`` that is always valid UTF-8 text: a lone surrogate becomes a backslash escape.

    A verb's message reaches the script as a Lua string; one that cannot be
    encoded would raise inside the reply builder or inside ``lupa``.
    """
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 -- a __str__ that raises
        text = type(value).__name__
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _worker_env() -> dict[str, str]:
    if sys.platform == "win32":
        return {"SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows")}
    return {}


def start_worker(argv: list[str]) -> subprocess.Popen[bytes]:
    """Start a worker (or a stand-in): empty environment, three pipes, no inherited descriptors."""
    creationflags = 0
    if sys.platform == "win32":
        from nexus.util import win_job  # noqa: PLC0415

        creationflags = win_job.CREATE_NO_WINDOW
    return subprocess.Popen(  # noqa: S603 -- argv is built here, never from a script
        argv_off_cwd(
            argv
        ),  # the head is sys.executable, absolute; a bare head is never taken from the cwd
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_worker_env(),
        cwd=tempfile.gettempdir(),
        close_fds=True,
        creationflags=creationflags,
    )


def worker_argv(limits: oslimits.OsLimits, reply_cap: int | None = None) -> list[str]:
    extra = [] if reply_cap is None else ["--reply-cap", str(reply_cap)]
    return [
        sys.executable,
        "-I",
        "-m",
        "nexus.lua_runtime.worker",
        *limits.to_args(),
        *extra,
    ]


def _reap(proc: subprocess.Popen[bytes], job: _Job | None) -> int:
    try:
        return proc.wait(timeout=EXIT_GRACE_S)
    except subprocess.TimeoutExpired:
        _kill(proc, job)
        return proc.wait()


def supervise(
    start: Mapping[str, JSONValue],
    *,
    limits: Limits = DEFAULT_LIMITS,
    verbs: Mapping[str, VerbSpec] | None = None,
    config: WorkerConfig = DEFAULT_CONFIG,
) -> RunOutcome:
    """Run one worker to its end.

    *start* holds the start frame's ``name``, ``source``, ``grants`` and
    ``args`` (``header.prepare_args``'s result; ``{}`` when absent); this
    adds ``verbs`` (every verb name in the table) and ``limits``. Raises
    ``ScriptBusyError`` when the client already runs four workers, and
    otherwise one of ``ScriptError``, ``ScriptLimitError`` or
    ``ScriptWorkerError``; the first two carry the worker's stats when it
    sent them.
    """
    if not _slots.acquire(blocking=False):
        raise ScriptBusyError(f"{MAX_WORKERS} script workers are already running")
    try:
        return _supervise(start, limits, verbs, config)
    finally:
        _slots.release()


def _supervise(
    start: Mapping[str, JSONValue],
    limits: Limits,
    verbs: Mapping[str, VerbSpec] | None,
    config: WorkerConfig,
) -> RunOutcome:
    if verbs is None:
        from nexus.lua_runtime.verbs import VERB_TABLE  # noqa: PLC0415

        verbs = VERB_TABLE
    grants = frozenset(str(g) for g in (start.get("grants") or []))  # type: ignore[union-attr]
    deadline = time.monotonic() + limits.wall_time
    cap = (
        config.verb_frame_cap
        if config.verb_frame_cap is not None
        else verb_types.VERB_RESULT_CAP_BYTES
    )
    os_limits = oslimits.limits_for(
        limits.wall_time,
        cpu_margins=config.cpu_margins,
        as_bytes=config.os_memory_bytes,
    )
    argv = (
        list(config.argv)
        if config.argv is not None
        else worker_argv(os_limits, frames.reply_frame_cap(cap))
    )
    job: _Job | None = None
    proc: subprocess.Popen[bytes] | None = None
    keeper: _StderrKeeper | None = None
    timer: _KillTimer | None = None
    try:
        if sys.platform == "win32":
            from nexus.util import win_job  # noqa: PLC0415

            handle = win_job.create_job(
                per_process_user_time_100ns=os_limits.cpu_soft_s * 10_000_000,
                process_memory_limit=os_limits.as_bytes,
            )
            if handle is None:
                raise ScriptWorkerError("cannot create the worker's job object")
            job = _Job(handle)
        try:
            proc = start_worker(argv)
        except OSError as exc:
            raise ScriptWorkerError(f"cannot start the worker: {exc}") from exc
        if job is not None and not job.assign(proc.pid):
            raise ScriptWorkerError("cannot put the worker in its job object")
        keeper = _StderrKeeper(proc.stderr)
        keeper.start()
        wake = threading.Event()
        timer = _KillTimer(proc, job, limits.wall_time, wake)
        timer.start()
        if config.on_spawn is not None:
            config.on_spawn(proc)
        return _converse(
            proc,
            job,
            keeper,
            timer,
            wake,
            start,
            limits,
            verbs,
            grants,
            deadline,
            cap,
            config,
            os_limits,
        )
    finally:
        # One cleanup from the first resource on: whatever failed, and wherever, the process is ended, the pipes and
        # the job handle are closed, and the child is reaped.
        if timer is not None:
            timer.cancel()
        if proc is not None:
            _kill(proc, job)
            if keeper is not None:
                keeper.join(
                    timeout=2.0
                )  # the drain ends when the killed worker's stderr reaches end of file
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            proc.wait()
        elif job is not None:
            job.close()


def _converse(
    proc: subprocess.Popen[bytes],
    job: _Job | None,
    keeper: _StderrKeeper,
    timer: _KillTimer,
    wake: threading.Event,
    start: Mapping[str, JSONValue],
    limits: Limits,
    verbs: Mapping[str, VerbSpec],
    grants: frozenset[str],
    deadline: float,
    cap: int,
    config: WorkerConfig,
    os_limits: oslimits.OsLimits,
) -> RunOutcome:
    assert proc.stdin is not None and proc.stdout is not None

    def worker_error(detail: str) -> ScriptWorkerError:
        _kill(proc, job)
        rc = proc.wait()
        return ScriptWorkerError(
            detail,
            returncode=rc if rc >= 0 else None,
            signal=-rc if rc < 0 else None,
            stderr_head=keeper.head(),
        )

    def ended() -> Exception:
        """The error for a worker that ended without a final frame (the ends table)."""
        rc = _reap(proc, job)
        if timer.fired:
            return ScriptLimitError("wall_time", limits.wall_time)
        if sys.platform != "win32" and rc == -signal.SIGXCPU:
            return ScriptLimitError("cpu_time", os_limits.cpu_soft_s)
        if job is not None and job.terminated_by_limit():
            return ScriptLimitError("cpu_time", os_limits.cpu_soft_s)
        if rc == EXIT_MEMORY:
            return ScriptLimitError("memory", os_limits.as_bytes)
        return ScriptWorkerError(
            "the worker ended without a result"
            + (f" (exit {rc})" if rc >= 0 else f" (signal {-rc})"),
            returncode=rc if rc >= 0 else None,
            signal=-rc if rc < 0 else None,
            stderr_head=keeper.head(),
        )

    try:
        return _protocol(
            proc,
            job,
            keeper,
            timer,
            wake,
            start,
            limits,
            verbs,
            grants,
            deadline,
            cap,
            config,
            worker_error,
            ended,
        )
    except ScriptRunError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a surprise in the run loop is the worker's problem, not an escaping exception
        _log.warning("lua_run_loop_failed", exc_class=type(exc).__name__, exc_info=True)
        raise worker_error(
            f"internal error in the run loop ({type(exc).__name__})"
        ) from None


def _protocol(
    proc: subprocess.Popen[bytes],
    job: _Job | None,
    keeper: _StderrKeeper,
    timer: _KillTimer,
    wake: threading.Event,
    start: Mapping[str, JSONValue],
    limits: Limits,
    verbs: Mapping[str, VerbSpec],
    grants: frozenset[str],
    deadline: float,
    cap: int,
    config: WorkerConfig,
    worker_error: Callable[[str], ScriptWorkerError],
    ended: Callable[[], Exception],
) -> RunOutcome:
    assert proc.stdin is not None and proc.stdout is not None
    try:
        ready = frames.read_frame(proc.stdout, frames.READY_FRAME_CAP)
    except frames.FrameError as exc:
        if exc.kind == "eof":
            raise ended() from None
        raise worker_error(f"bad first frame from the worker: {exc}") from None
    if ready.get("t") != "ready":
        raise worker_error(f"the worker's first frame is {ready.get('t')!r}, not ready")

    start_frame: dict[str, Any] = {
        "t": "start",
        "args": {},
        **dict(start),
        "verbs": sorted(verbs),
        "limits": limits.to_frame(),
    }
    try:
        frames.write_frame(proc.stdin, start_frame, frames.START_FRAME_CAP)
    except frames.FrameError as exc:
        raise worker_error(f"the start frame cannot be sent: {exc}") from None
    except OSError:
        raise ended() from None

    calls = 0
    while True:
        try:
            frame, size = frames.read_frame_sized(proc.stdout, frames.END_FRAME_CAP)
        except frames.FrameError as exc:
            if exc.kind == "eof" or timer.fired:
                raise ended() from None
            raise worker_error(f"bad frame from the worker: {exc}") from None
        kind = frame["t"]
        if kind == "call":
            calls += 1
            if calls > config.max_calls:
                raise worker_error(
                    f"the script made more than {config.max_calls} verb calls"
                )
            if size > frames.CALL_FRAME_CAP:
                raise worker_error(
                    f"a verb-call frame is over the {frames.CALL_FRAME_CAP}-byte cap"
                )
            call_id = frame.get("id")
            if (
                not isinstance(call_id, int)
                or isinstance(call_id, bool)
                or not isinstance(frame.get("verb"), str)
                or not isinstance(frame.get("args"), list)
            ):
                raise worker_error(
                    "malformed call frame: an integer id, a verb name and a list of arguments"
                )
            reply = _serve_call(
                frame, verbs, grants, deadline, cap, config.ctx, wake, timer, proc
            )
            if (
                reply is None
            ):  # the timer ended the worker, the verb timed out, or the worker died during the verb
                raise ended()
            try:
                proc.stdin.write(reply)
                proc.stdin.flush()
            except (OSError, ValueError):
                raise ended() from None
            continue
        if not timer.cancel():
            raise ended()
        if kind not in ("result", "error", "limit"):
            raise worker_error(f"unexpected {kind!r} frame from the worker")
        rc = _reap(proc, job)
        if rc != 0:
            raise ScriptWorkerError(
                f"the worker sent {kind!r} and then exited {rc}",
                returncode=rc if rc >= 0 else None,
                signal=-rc if rc < 0 else None,
                stderr_head=keeper.head(),
            )
        stats = _stats(frame)
        if kind == "result":
            return RunOutcome(frame.get("value"), stats or {})
        if kind == "error":
            err_kind = frame.get("kind")
            # isinstance first: a list or dict is unhashable and `in` on a frozenset would raise TypeError
            if (
                not isinstance(err_kind, str)
                or err_kind not in _WORKER_KINDS
                or err_kind not in KINDS
            ):
                raise worker_error(f"error frame with kind {err_kind!r}")
            raise ScriptError(err_kind, _text(frame.get("detail", "")), stats=stats)
        limit = frame.get("limit")
        if not isinstance(limit, str) or limit not in _WORKER_LIMITS:
            raise worker_error(f"limit frame with limit {limit!r}")
        raise ScriptLimitError(limit, frame.get("value"), stats=stats)


def _reply_bytes(
    call_id: Any,
    ok: bool,
    *,
    value_json: bytes | None = None,
    error: tuple[str, str] | None = None,
) -> bytes:
    """A whole ``reply`` frame; the verb result is spliced in as the bytes ``encode_result`` made."""
    head = f'{{"t":"reply","id":{json.dumps(call_id)},"ok":{"true" if ok else "false"},'.encode()
    if ok:
        assert value_json is not None
        body = head + b'"value":' + value_json + b"}"
    else:
        assert error is not None
        kind, message = error
        # ensure_ascii: a message may hold a lone surrogate, which UTF-8 cannot carry but a \u escape can
        body = (
            head
            + b'"error":'
            + json.dumps(
                {"kind": kind, "message": _text(message)}, separators=(",", ":")
            ).encode("ascii")
            + b"}"
        )
    return len(body).to_bytes(4, "big") + body


def _serve_call(
    frame: Mapping[str, Any],
    verbs: Mapping[str, VerbSpec],
    grants: frozenset[str],
    deadline: float,
    cap: int,
    ctx: VerbContext,
    wake: threading.Event,
    timer: _KillTimer,
    proc: subprocess.Popen[bytes],
) -> bytes | None:
    """The reply frame for one call, or None when the run ended first (wall time, or the worker itself)."""
    call_id = frame.get("id")
    name = frame.get("verb")
    args = frame.get("args")

    def refuse(kind: str, message: str) -> bytes:
        return _reply_bytes(call_id, False, error=(kind, message))

    if not isinstance(name, str) or name not in grants or name not in verbs:
        return refuse("ungranted", f"verb {name} is not granted to this script")
    spec = verbs[name]
    try:
        checked = check_args(spec, args)
    except VerbArgError as exc:
        return refuse("bad_args", str(exc))
    except VerbError as exc:
        return refuse("verb_error", str(exc))
    wake.clear()
    call = _VerbCall(spec, checked, deadline, ctx, cap, wake)
    call.start()
    while True:
        wake.clear()
        if timer.fired:
            return None  # abandon the verb: its thread ends alone and its result is dropped
        if call.finished:
            break
        if proc.poll() is not None:
            return None  # the worker died inside the verb: nobody is waiting for the result
        wake.wait(VERB_POLL_S)
    if call.timed_out:
        timer.force()
        return None
    if call.error is not None:
        return refuse("verb_error", call.error)
    assert call.data is not None
    return _reply_bytes(call_id, True, value_json=call.data)
