# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 2, fix round r1 (nexus-mdbds.4): what the review found a hostile or unlucky run could do.

Each test here failed on the code of the first commit and is named for the
hole it closes: a worker that dies inside a verb that never returns (B1), a
verb thread that dies without telling the run loop (B2), a worker frame whose
fields are not the types the parent assumed (B3), a final frame that cannot be
encoded (B4), limits that are not numbers a limit can be (B5), a verb result
whose table costs more than the build assumed (N1), a spawn that leaks a
descriptor (N2), a run that ends badly and keeps its slot (N4), a failure
between ``Popen`` and the run loop (N5), and the oversize ready and start
frames and the worker's own reply cap (N8).
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import textwrap
import threading
import time
from typing import Any

import pytest

from nexus.lua_runtime import frames, oslimits, supervisor
from nexus.lua_runtime.errors import ScriptError, ScriptLimitError, ScriptWorkerError
from nexus.lua_runtime.limits import Limits
from nexus.lua_runtime.supervisor import WorkerConfig, supervise
from tests.lua_runtime._support import (
    named_in as _named_in,
    fake_worker,
    limits,
    ready_then,
    start,
    verb,
    verb_table,
)

_ECHO = verb("fixture.echo", lambda s, deadline, ctx: s, "string")
_TABLE = verb_table(_ECHO)


def _run(
    source: str,
    *,
    wall: float = 20.0,
    verbs: Any = None,
    config: WorkerConfig | None = None,
):
    return supervise(
        start(source, *_named_in(source, verbs or _TABLE)),
        limits=limits(wall),
        verbs=verbs or _TABLE,
        config=config or WorkerConfig(),
    )


def _worker_error(body: str, wall: float = 30.0) -> ScriptWorkerError:
    with pytest.raises(ScriptWorkerError) as err:
        supervise(
            start("-", "fixture.echo"),
            limits=limits(wall),
            verbs=_TABLE,
            config=WorkerConfig(argv=fake_worker(body)),
        )
    return err.value


# ── B1: a worker that dies inside a verb that never returns ─────────────────


def test_a_worker_killed_inside_a_verb_that_never_returns_ends_the_run_at_once() -> (
    None
):
    released = threading.Event()
    entered = threading.Event()
    procs: list[subprocess.Popen[bytes]] = []

    def never(deadline: float, ctx: Any) -> Any:
        entered.set()
        released.wait(
            8
        )  # "never": longer than the budget below, short enough that a red run ends
        return "late"

    def kill_worker() -> None:
        assert entered.wait(30)
        procs[0].kill()

    threading.Thread(target=kill_worker, daemon=True).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(ScriptWorkerError) as err:
            supervise(
                start("return fixture.never()", "fixture.never"),
                limits=limits(30),
                verbs=verb_table(verb("fixture.never", never)),
                config=WorkerConfig(on_spawn=procs.append),
            )
    finally:
        released.set()
    took = time.monotonic() - t0
    assert took < 3, f"supervise waited {took:.1f}s on a verb after its worker was dead"
    assert "without a result" in err.value.detail


# ── B2: a verb thread must always tell the run loop it is done ──────────────


def test_a_verb_error_message_with_a_lone_surrogate_is_an_error_reply_not_a_hang_or_a_false_wall_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnwritableLog:
        """A log whose sink cannot encode the record, as a console on a strict codec cannot."""

        def warning(self, *args: Any, **kwargs: Any) -> None:
            raise UnicodeEncodeError("ascii", "\udc80", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(supervisor, "_log", UnwritableLog())

    def odd(deadline: float, ctx: Any) -> Any:
        raise RuntimeError("bad \udc80 name")

    table = verb_table(verb("fixture.odd", odd))
    t0 = time.monotonic()
    out = _run(
        "local ok, e = pcall(fixture.odd) return {ok = ok, e = e}",
        wall=6,
        verbs=table,
    )
    assert (
        out.value["ok"] is False
        and "bad" in out.value["e"]
        and "name" in out.value["e"]
    )
    assert time.monotonic() - t0 < 5


def test_a_verb_that_raises_a_baseexception_is_an_error_reply_not_a_dead_thread() -> (
    None
):
    class Bail(BaseException):
        pass

    def bail(deadline: float, ctx: Any) -> Any:
        raise Bail("bailing")

    table = verb_table(verb("fixture.bail", bail))
    t0 = time.monotonic()
    out = _run(
        "local ok, e = pcall(fixture.bail) return {ok = ok, e = e}", wall=6, verbs=table
    )
    assert out.value["ok"] is False and "bailing" in out.value["e"]
    assert time.monotonic() - t0 < 5


# ── B3: worker frames whose fields are not the types the parent assumed ─────


@pytest.mark.parametrize(
    "frame",
    [
        '{"t": "error", "kind": ["lua"], "detail": "x"}',
        '{"t": "error", "kind": {"a": 1}, "detail": "x"}',
        '{"t": "limit", "limit": ["memory"], "value": 1}',
        '{"t": "limit", "limit": {"a": 1}, "value": 1}',
        '{"t": "error", "kind": 5, "detail": "x"}',
    ],
    ids=[
        "error-kind-list",
        "error-kind-dict",
        "limit-list",
        "limit-dict",
        "error-kind-int",
    ],
)
def test_an_error_or_limit_frame_with_a_field_of_the_wrong_type_is_a_worker_error(
    frame: str,
) -> None:
    with pytest.raises(ScriptWorkerError) as err:
        supervise(
            start("-"),
            limits=limits(30),
            verbs=_TABLE,
            config=WorkerConfig(argv=ready_then(f"send({frame})")),
        )
    assert "frame with" in err.value.detail


def test_a_call_frame_naming_a_verb_with_a_lone_surrogate_is_refused_not_a_crash() -> (
    None
):
    body = (
        'send({"t": "ready"}); recv()\n'
        'payload = b\'{"t":"call","id":1,"verb":"\\\\ud800","args":[]}\'\n'
        "raw(header(len(payload)) + payload)\n"
        "reply = recv()\n"
        'send({"t": "result", "value": reply})\n'
    )
    out = supervise(
        start("-", "fixture.echo"),
        limits=limits(30),
        verbs=_TABLE,
        config=WorkerConfig(argv=fake_worker(body)),
    )
    assert out.value["ok"] is False and out.value["error"]["kind"] == "ungranted"


def test_a_call_frame_whose_id_is_not_an_integer_is_a_worker_error() -> None:
    err = _worker_error(
        'send({"t": "ready"}); recv(); send({"t": "call", "id": [1], "verb": "fixture.echo", "args": ["x"]}); time.sleep(30)'
    )
    assert "call frame" in err.detail


@pytest.mark.parametrize(
    "fields",
    [
        '"verb": 5, "args": ["x"]',
        '"verb": ["fixture.echo"], "args": ["x"]',
        '"verb": null, "args": ["x"]',
        '"verb": "fixture.echo", "args": "x"',
        '"verb": "fixture.echo", "args": {"a": 1}',
        '"verb": "fixture.echo", "args": null',
    ],
    ids=[
        "verb-int",
        "verb-list",
        "verb-null",
        "args-string",
        "args-object",
        "args-null",
    ],
)
def test_a_call_frame_whose_verb_is_not_a_string_or_whose_args_is_not_a_list_is_a_worker_error(
    fields: str,
) -> None:
    t0 = time.monotonic()
    err = _worker_error(
        'send({"t": "ready"}); recv()\n'
        f'P = b\'{{"t":"call","id":1,{fields}}}\'\n'
        "raw(header(len(P)) + P)\n"
        "time.sleep(30)\n"
    )
    assert "malformed call frame" in err.detail
    assert time.monotonic() - t0 < 10  # refused at once, not left to the wall limit


def test_a_surprise_inside_the_run_loop_is_a_worker_error_not_an_escaping_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise ZeroDivisionError("surprise")

    monkeypatch.setattr(supervisor, "_serve_call", boom)
    err = _worker_error(
        'send({"t": "ready"}); recv(); send({"t": "call", "id": 1, "verb": "fixture.echo", "args": ["x"]}); time.sleep(30)'
    )
    assert "ZeroDivisionError" in err.detail or "internal" in err.detail


# ── B4: a final frame that cannot be sent ───────────────────────────────────


def test_a_nan_result_is_a_result_error_not_a_dead_worker() -> None:
    with pytest.raises(ScriptError) as err:
        _run("return 0/0")
    assert err.value.kind == "result" and "JSON" in err.value.detail


def test_an_infinite_result_is_a_result_error_not_a_dead_worker() -> None:
    with pytest.raises(ScriptError) as err:
        _run("return 1/0")
    assert err.value.kind == "result" and "JSON" in err.value.detail


def test_a_lua_error_message_over_the_frame_cap_is_truncated_not_a_dead_worker() -> (
    None
):
    with pytest.raises(ScriptError) as err:
        _run('error(("x"):rep(2000000))')
    assert err.value.kind == "lua"
    assert 1000 < len(err.value.detail) <= 64 * 1024
    assert err.value.detail.startswith("script:1:")


# ── B5: limits must be numbers a limit can be ───────────────────────────────


@pytest.mark.parametrize(
    "field", ["wall_time", "memory", "instructions", "result", "depth"]
)
@pytest.mark.parametrize(
    "bad",
    [math.inf, -math.inf, math.nan, 0, -1],
    ids=["inf", "-inf", "nan", "zero", "negative"],
)
def test_a_limit_that_is_not_finite_and_positive_is_refused_at_construction(
    field: str, bad: float
) -> None:
    with pytest.raises(ValueError):
        Limits(**{field: bad})


@pytest.mark.parametrize(
    "field", ["wall_time", "memory", "instructions", "result", "depth"]
)
@pytest.mark.parametrize(
    "bad", [math.nan, -math.inf, 0, -1], ids=["nan", "-inf", "zero", "negative"]
)
def test_lowering_a_limit_to_nothing_or_nan_is_refused(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        Limits().lowered(**{field: bad})


def test_the_defaults_and_an_ordinary_lowering_are_still_fine() -> None:
    assert Limits().lowered(memory=1 << 20, wall_time=0.5).memory == 1 << 20
    assert Limits.from_frame(Limits().to_frame()) == Limits()


# ── N1: a verb result whose table costs more than the build assumed ─────────


def test_3400_chains_of_depth_300_are_a_memory_limit_not_an_abort() -> None:
    """~2 MB of JSON at 36 Lua bytes per byte is over 64 MB; the build bound used to be 32."""
    chain: list[Any] = []
    for _ in range(299):
        chain = [chain]
    table = verb_table(verb("fixture.deep", lambda deadline, ctx: [chain] * 3400))
    with pytest.raises(ScriptLimitError) as err:
        _run(
            "return #fixture.deep()",
            verbs=table,
            config=WorkerConfig(verb_frame_cap=8 * 1024 * 1024),
        )
    assert err.value.limit == "memory", err.value
    assert _run("return 1").value == 1


def test_a_result_nested_far_past_the_result_depth_still_reaches_the_script_without_killing_the_worker() -> (
    None
):
    deep: list[Any] = []
    for _ in range(2000):
        deep = [deep]
    table = verb_table(verb("fixture.abyss", lambda deadline, ctx: deep))
    out = _run("return type(fixture.abyss())", verbs=table)
    assert out.value == "table"


# ── N2: a spawn must not leak a descriptor open in the parent ───────────────

_LEAK_PROBE = textwrap.dedent(
    """
    import json, os, sys
    from nexus.lua_runtime import supervisor
    r0, w0 = os.pipe()
    # high numbers no descriptor of the probing child itself can have, and inheritable on purpose
    r, w = os.dup2(r0, 50, inheritable=True), os.dup2(w0, 51, inheritable=True)
    probe = (
        "import json, os, sys\\n"
        "d = '/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'\\n"
        "fds = sorted(int(e) for e in os.listdir(d))\\n"
        "json.dump(fds, sys.stdout)\\n"
    )
    proc = supervisor.start_worker([sys.executable, "-I", "-c", probe])
    out, _ = proc.communicate(timeout=60)
    json.dump({"leaked": [r, w], "child": json.loads(out)}, sys.stdout)
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptors")
def test_an_inheritable_descriptor_open_in_the_parent_does_not_reach_the_worker() -> (
    None
):
    """Run in a bare child: the test session's own spawn plugin (macOS) makes descriptors non-inheritable first."""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _LEAK_PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    # the child lists its own descriptors, including the one it lists them with
    seen_beyond_std = [fd for fd in got["child"] if fd > 2]
    assert not set(got["leaked"]) & set(seen_beyond_std), (
        f"the worker holds the parent's descriptors {got['leaked']}: {got['child']}"
    )


# ── N4: every way a run can end gives its slot back ─────────────────────────


def _slots_are_free() -> bool:
    got = [
        supervisor._slots.acquire(blocking=False) for _ in range(supervisor.MAX_WORKERS)
    ]
    for ok in got:
        if ok:
            supervisor._slots.release()
    return all(got)


def test_no_end_path_keeps_a_slot_five_runs_each() -> None:
    def explode(deadline: float, ctx: Any) -> Any:
        raise RuntimeError("explodes")

    explosive = verb_table(verb("fixture.explode", explode))
    for _ in range(5):
        with pytest.raises(ScriptLimitError):  # the timer
            _run("while true do end", wall=0.5)
        out = _run(
            "local ok = pcall(fixture.explode) return ok", verbs=explosive
        )  # a verb exception
        assert out.value is False
        _worker_error('send({"t": "ready"}); recv(); sys.exit(9)')  # a crash
        _worker_error(
            'send({"t": "ready"}); recv(); raw(header(5) + b"hello"); time.sleep(30)'
        )  # a bad frame
    assert _slots_are_free(), "a slot leaked on one of the end paths"


# ── N5: a failure between Popen and the run loop leaks nothing ──────────────


def test_a_failure_after_the_spawn_kills_the_worker_and_frees_the_slot() -> None:
    procs: list[subprocess.Popen[bytes]] = []

    def spawn_hook(proc: subprocess.Popen[bytes]) -> None:
        procs.append(proc)
        raise RuntimeError("the spawn hook failed")

    with pytest.raises(RuntimeError, match="spawn hook failed"):
        supervise(
            start("return 1"),
            limits=limits(30),
            verbs=_TABLE,
            config=WorkerConfig(on_spawn=spawn_hook),
        )
    proc = procs[0]
    assert proc.poll() is not None, "the worker was left running"
    assert proc.stdin.closed and proc.stdout.closed and proc.stderr.closed, (
        "pipes left open"
    )
    assert _slots_are_free()


# ── N8: oversize ready and start frames, and the worker's own reply cap ─────


def test_a_ready_frame_over_its_cap_ends_the_run_naming_the_first_frame() -> None:
    err = _worker_error(
        "raw(header(frames.READY_FRAME_CAP + 1)); time.sleep(30)", wall=5
    )
    assert "first frame" in err.detail and "cap" in err.detail


def test_a_start_frame_over_its_cap_is_refused_by_the_parent() -> None:
    huge = "x" * (frames.START_FRAME_CAP + 10)
    with pytest.raises(ScriptWorkerError) as err:
        supervise(
            start(huge),
            limits=limits(5),
            verbs=_TABLE,
            config=WorkerConfig(argv=ready_then("time.sleep(30)")),
        )
    assert "start frame" in err.value.detail


def _real_worker(*extra: str) -> subprocess.Popen[bytes]:
    return supervisor.start_worker(
        supervisor.worker_argv(oslimits.limits_for(60)) + list(extra)
    )


def _stop(proc: subprocess.Popen[bytes]) -> None:
    proc.kill()
    for s in (proc.stdin, proc.stdout, proc.stderr):
        s.close()
    proc.wait()


def test_the_worker_refuses_a_start_frame_over_its_cap() -> None:
    proc = _real_worker()
    try:
        assert frames.read_frame(proc.stdout, frames.READY_FRAME_CAP) == {"t": "ready"}
        proc.stdin.write((frames.START_FRAME_CAP + 1).to_bytes(4, "big"))
        proc.stdin.flush()
        assert proc.wait(timeout=30) == 6
        assert b"bad start frame" in proc.stderr.read()
    finally:
        _stop(proc)


def test_the_worker_enforces_its_own_reply_cap() -> None:
    proc = _real_worker("--reply-cap", "100")
    try:
        assert frames.read_frame(proc.stdout, frames.READY_FRAME_CAP) == {"t": "ready"}
        frames.write_frame(
            proc.stdin,
            {
                "t": "start",
                "name": "n",
                "source": 'return fixture.echo("x")',
                "grants": ["fixture.echo"],
                "verbs": ["fixture.echo"],
                "args": {},
                "limits": Limits().to_frame(),
            },
            frames.START_FRAME_CAP,
        )
        call = frames.read_frame(proc.stdout, frames.CALL_FRAME_CAP)
        assert call["t"] == "call"
        frames.write_frame(
            proc.stdin,
            {"t": "reply", "id": call["id"], "ok": True, "value": "y" * 500},
            frames.START_FRAME_CAP,
        )
        assert proc.wait(timeout=30) == 6, (
            "a 500-byte reply got through a 100-byte reply cap"
        )
        assert b"bad reply frame" in proc.stderr.read()
    finally:
        _stop(proc)


def test_a_huge_finite_wall_time_keeps_the_timer_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Limits accepts any finite positive wall_time, but Event.wait raises
    # OverflowError past threading.TIMEOUT_MAX (about 4.3e6 s on Windows), and
    # an exception in the timer thread would silently end wall-time
    # enforcement (confirmation review of 9012c953e).
    raised: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda a: raised.append(a.exc_value))
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
    )
    try:
        timer = supervisor._KillTimer(proc, None, 1e12, threading.Event())
        timer.start()
        time.sleep(0.3)
        assert timer.is_alive(), f"the timer thread died: {raised}"
        assert timer.cancel()
        timer.join(5)
        assert not timer.is_alive()
        assert raised == []
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("memory", [1, 4096, 130_000, (1 << 20) - 1])
def test_a_memory_limit_below_the_floor_is_refused(memory: int) -> None:
    # Below about 130 KB, lupa aborts the worker (exit 134, "PANIC: unprotected
    # error in call to Lua API (not enough memory)") while it pushes the source
    # and builds the runtime outside any protected call; a floor of 1 MiB keeps
    # every run above that (nexus-mdbds.5 review r1).
    with pytest.raises(ValueError, match="memory"):
        Limits(memory=memory)
    with pytest.raises(ValueError, match="memory"):
        Limits().lowered(memory=memory)


def test_the_memory_floor_itself_runs_a_script() -> None:
    assert Limits().lowered(memory=1 << 20).memory == 1 << 20
    out = supervise(
        start("return 1 + 1"),
        limits=Limits().lowered(memory=1 << 20, wall_time=20),
        verbs=_TABLE,
        config=WorkerConfig(),
    )
    assert out.value == 2
