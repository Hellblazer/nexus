# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The sandbox worker: ``python -I -m nexus.lua_runtime.worker`` (RDR-242 Technical Design 6).

The parent starts one worker per run with an empty environment and three
pipes. The worker, in this order: sets its own OS limits (``oslimits``),
imports ``lupa``, sends ``{"t": "ready"}``, and then reads nothing and runs
nothing until the parent's first frame (``start``), which the parent sends
after any limit it sets from outside (the Windows job). Frames are 4-byte
length-prefixed JSON (``frames``). A verb call is a ``call`` frame and the
parent's ``reply``; the verbs run in the parent and the worker never imports
a verb module. The worker runs the static check (when ``static_check``
exists), then ``runtime.run`` (Step 3's), and turns what it returns or
raises into the one final frame.

Arguments after the module name are the OS limits and the reply cap
(``--cpu-soft``, ``--cpu-hard``, ``--as-bytes``, ``--reply-cap``): the
environment is empty and the worker may read nothing before it has set them.

Exit codes: 0 after the final frame, or when the parent closes the pipe
before or between frames; 3 on ``MemoryError`` (the OS memory limit);
4 when an OS limit cannot be set (before the source is read); 5 when
``lupa`` cannot be imported; 6 on a protocol violation by the parent.
Exit 3 is also what the Microsoft C runtime's ``abort()`` returns, so on
Windows a Lua ``PANIC`` abort is filed as the memory limit, which is what it
is (an allocation the allocator refused); on POSIX it is SIGABRT and a
``ScriptWorkerError``.

"Standard library and ``lupa`` only" holds on POSIX. On win32 ``nexus/__init__``
imports ``truststore`` (the OS certificate store) before this module loads,
so the worker's imports there are the package root's too; the worker uses
nothing from it.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any

from nexus.lua_runtime import frames as _frames
from nexus.lua_runtime import oslimits as _oslimits
from nexus.lua_runtime.errors import ScriptError, ScriptLimitError
from nexus.lua_runtime.limits import Limits
from nexus.lua_runtime.verb_types import VERB_RESULT_CAP_BYTES

#: Also the exit status of the Microsoft C runtime's abort(): see the module docstring.
EXIT_MEMORY: int = 3
EXIT_LIMIT_SETUP: int = _oslimits.EXIT_LIMIT_SETUP_FAILED
EXIT_NO_LUPA: int = 5
EXIT_PROTOCOL: int = 6

#: Characters of a Lua error message kept when the final frame would be over its cap (UTF-8 makes it up to 3x in bytes).
_DETAIL_KEEP_CHARS = 60 * 1024

#: The default reply cap when the parent passes none: the verb-result cap plus slack.
_DEFAULT_REPLY_CAP = _frames.reply_frame_cap(VERB_RESULT_CAP_BYTES)


def _die(code: int) -> None:
    """Leave without finalizers: ``lupa`` teardown is slow and has nothing to save."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 -- leaving
            pass
    os._exit(code)


def _static_check(
    source: bytes, grants: frozenset[str], verbs: frozenset[str]
) -> str | None:
    """Marlowe's check, when its module exists. ``check`` calibrates itself on first use.

    Returns the error-frame detail for a refusal (``str(refusal)``: rule, detail,
    line) or a calibration failure (``calibrate: <message>``).
    """
    try:
        static_check = importlib.import_module("nexus.lua_runtime.static_check")
    except ModuleNotFoundError as exc:
        if exc.name != "nexus.lua_runtime.static_check":
            raise
        return None
    try:
        static_check.check(source, grants, verbs)
    except static_check.StaticCheckRefusal as refusal:
        return str(refusal)
    except static_check.ChunkFormatError as exc:
        return f"calibrate: {exc}"
    return None


def _split_args(argv: list[str]) -> tuple[_oslimits.OsLimits, int]:
    reply_cap = _DEFAULT_REPLY_CAP
    rest: list[str] = []
    it = iter(argv)
    for flag in it:
        if flag == "--reply-cap":
            reply_cap = int(next(it))
        else:
            rest.append(flag)
            rest.append(next(it))
    return _oslimits.OsLimits.from_args(rest), reply_cap


def _serve(stdin: Any, stdout: Any, start: dict[str, Any], reply_cap: int) -> None:
    from nexus.lua_runtime import runtime  # noqa: PLC0415

    def final(frame: dict[str, Any]) -> None:
        """Send the one final frame; a frame that cannot be sent becomes one that can, never a traceback."""
        try:
            _frames.write_frame(stdout, frame, _frames.END_FRAME_CAP)
            return
        except _frames.FrameError as exc:
            problem = exc
        stats = frame.get("stats") or {}
        if problem.kind == "too_large" and frame.get("t") == "error":
            # a Lua error message of megabytes: keep its head
            detail = str(frame.get("detail", ""))
            retry: dict[str, Any] = {
                **frame,
                "detail": detail[:_DETAIL_KEEP_CHARS] + " ... [truncated]",
            }
        elif problem.kind == "too_large":
            retry = {
                "t": "limit",
                "limit": "result",
                "value": int(limits.result),
                "stats": stats,
            }
        else:
            # NaN or infinity in the result (JSON has neither), or a value json cannot encode at all
            retry = {
                "t": "error",
                "kind": "result",
                "detail": f"result is not JSON-encodable: {problem.detail[:300]}",
                "stats": stats,
            }
        try:
            _frames.write_frame(stdout, retry, _frames.END_FRAME_CAP)
        except _frames.FrameError as exc:
            sys.stderr.write(f"worker: cannot send a final frame: {exc}\n")
            _die(EXIT_PROTOCOL)

    grants = frozenset(str(g) for g in start.get("grants") or [])
    verbs = frozenset(str(v) for v in start.get("verbs") or [])
    source = str(start.get("source", "")).encode("utf-8")
    try:
        limits = Limits.from_frame(start.get("limits") or {})
    except (ValueError, TypeError, OverflowError) as exc:
        sys.stderr.write(f"worker: bad limits in the start frame: {exc}\n")
        _die(EXIT_PROTOCOL)
    args = start.get("args") if isinstance(start.get("args"), dict) else {}

    refusal = _static_check(source, grants, verbs)
    if refusal is not None:
        final({"t": "error", "kind": "static_check", "detail": refusal, "stats": {}})
        return

    call_id = 0

    def call_verb(name: str, args: list[Any]) -> Any:
        nonlocal call_id
        call_id += 1
        try:
            _frames.write_frame(
                stdout,
                {"t": "call", "id": call_id, "verb": name, "args": args},
                _frames.CALL_FRAME_CAP,
            )
        except _frames.FrameError as exc:
            raise _frames.VerbReplyError(
                f"{name}: the call is too large to send ({exc.detail})"
            ) from None
        try:
            reply = _frames.read_frame(stdin, reply_cap)
        except _frames.FrameError as exc:
            if exc.kind == "eof":
                _die(0)  # the parent is gone: nothing to report to
            sys.stderr.write(f"worker: bad reply frame: {exc}\n")
            _die(EXIT_PROTOCOL)
        if reply.get("t") != "reply" or reply.get("id") != call_id:
            sys.stderr.write(f"worker: unexpected frame {reply.get('t')!r}\n")
            _die(EXIT_PROTOCOL)
        if reply.get("ok") is not True:
            error = reply.get("error") or {}
            raise _frames.VerbReplyError(str(error.get("message", "verb failed")))
        return reply.get("value")

    try:
        value, stats = runtime.run(source, grants, args, limits, call_verb)
    except ScriptLimitError as exc:
        final(
            {
                "t": "limit",
                "limit": exc.limit,
                "value": exc.value,
                "stats": dict(exc.stats or {}),
            }
        )
        return
    except ScriptError as exc:
        final(
            {
                "t": "error",
                "kind": exc.kind,
                "detail": exc.detail,
                "stats": dict(exc.stats or {}),
            }
        )
        return
    final({"t": "result", "value": value, "stats": dict(stats)})


def main(argv: list[str]) -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print can never corrupt a frame
    try:
        os_limits, reply_cap = _split_args(argv)
        _oslimits.apply_in_worker(os_limits)
    except MemoryError:
        return EXIT_MEMORY
    except Exception as exc:  # noqa: BLE001 -- any failure to set a limit ends the run
        sys.stderr.write(f"worker: cannot set OS limits: {type(exc).__name__}: {exc}\n")
        return EXIT_LIMIT_SETUP
    try:
        import lupa.lua54  # noqa: F401, PLC0415
    except ImportError as exc:
        sys.stderr.write(f"worker: cannot import lupa: {exc}\n")
        return EXIT_NO_LUPA
    try:
        _frames.write_frame(stdout, {"t": "ready"}, _frames.READY_FRAME_CAP)
        try:
            start = _frames.read_frame(stdin, _frames.START_FRAME_CAP)
        except _frames.FrameError as exc:
            if exc.kind == "eof":
                return 0  # the parent closed the pipe: nothing was asked of us
            sys.stderr.write(f"worker: bad start frame: {exc}\n")
            return EXIT_PROTOCOL
        if start.get("t") != "start":
            sys.stderr.write(f"worker: first frame is {start.get('t')!r}, not start\n")
            return EXIT_PROTOCOL
        _serve(stdin, stdout, start, reply_cap)
        return 0
    except MemoryError:
        return EXIT_MEMORY
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    _die(main(sys.argv[1:]))
