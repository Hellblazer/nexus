# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 2 (nexus-mdbds.4): the worker process, from the parent's side.

The Test Plan's "Worker process" and "Limit suite" bullets that belong to the
process and its frames: the start conditions, the verb round trip, the
parent's grant and argument checks, the frame caps and the call count, the
wall-time budget (including a verb that ignores its timeout, gate r3 S2), the
cap of four workers, and each row of the worker-end table.

Two kinds of worker run here. The real one
(``python -I -m nexus.lua_runtime.worker``) runs a one-line Lua fixture. A
stand-in (``fake_worker``) speaks the frame protocol from a ``python -I -c``
program, so a test can send what an honest worker never would.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from typing import Any

import pytest

from nexus.lua_runtime import frames, supervisor
from nexus.lua_runtime.errors import (
    ScriptBusyError,
    ScriptError,
    ScriptLimitError,
    ScriptWorkerError,
)
from nexus.lua_runtime.limits import Limits
from nexus.lua_runtime.supervisor import WorkerConfig, supervise
from nexus.lua_runtime.verb_types import VERB_RESULT_CAP_BYTES, VerbError, VerbTimeout
from tests.lua_runtime._support import (
    Gate,
    fake_worker,
    limits,
    platform_only,
    ready_then,
    named_in as _named_in,
    start,
    verb,
    verb_table,
)

_ECHO = verb(
    "fixture.echo", lambda s, deadline, ctx: {"got": s, "list": [1, 2, 3]}, "string"
)
_TABLE = verb_table(_ECHO)


def _run(
    source: str,
    *,
    wall: float = 20.0,
    verbs: Any = None,
    config: WorkerConfig | None = None,
    **lowered: int,
):
    return supervise(
        start(source, *_named_in(source, verbs or _TABLE)),
        limits=limits(wall, **lowered),
        verbs=verbs or _TABLE,
        config=config or WorkerConfig(),
    )


# ── start conditions ────────────────────────────────────────────────────────

_PROBE = """
import json, os, sys, tempfile
fds = []
for d in ("/proc/self/fd", "/dev/fd"):
    if os.path.isdir(d):
        with os.scandir(d) as it:
            fds = sorted(int(e.name) for e in it)
        break
json.dump({"env": dict(os.environ), "cwd": os.getcwd(), "tmp": tempfile.gettempdir(), "fds": fds,
           "posix": sys.platform != "win32"}, sys.stdout)
"""


#: The parent passes env={}. What may still show: the interpreter's locale coercion (PEP 538) and macOS's
#: CoreFoundation add LC_CTYPE and __CF_USER_TEXT_ENCODING to any process, and tests/_posix_spawn.py
#: (macOS) starts a child with a cwd through /bin/sh, which sets PWD, OLDPWD and SHLVL. None carries a parent variable.
_ADDED_BY_INTERPRETER_OR_HARNESS = {
    "LC_CTYPE",
    "__CF_USER_TEXT_ENCODING",
    "PWD",
    "OLDPWD",
    "SHLVL",
    "_",
}


def test_the_worker_starts_with_an_empty_environment_no_extra_descriptor_and_the_temp_cwd() -> (
    None
):
    # A descriptor open in this test process must not reach the child: the pipe end is inheritable-looking.
    leak_r, leak_w = os.pipe()
    try:
        proc = supervisor.start_worker([sys.executable, "-I", "-c", _PROBE])
        out, err = proc.communicate(timeout=60)
    finally:
        os.close(leak_r)
        os.close(leak_w)
    assert proc.returncode == 0, err.decode()
    got = json.loads(out)
    if sys.platform == "win32":
        assert (
            set(got["env"]) <= {"SYSTEMROOT", "SYSTEMDRIVE"}
            and "SYSTEMROOT" in got["env"]
        ), got["env"]
    else:
        assert set(got["env"]) <= _ADDED_BY_INTERPRETER_OR_HARNESS, got["env"]
    assert os.path.samefile(got["cwd"], tempfile.gettempdir()), (
        got
    )  # the parent's temp dir, not the child's own
    if got["posix"]:
        # the three pipes, and the directory listing's own descriptor
        assert [fd for fd in got["fds"] if fd > 2][:1] in ([], got["fds"][3:4]), got[
            "fds"
        ]
        assert len([fd for fd in got["fds"] if fd > 2]) <= 1, (
            f"descriptors beyond the pipes: {got['fds']}"
        )


def test_the_environment_of_the_parent_does_not_reach_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_TENANT_TOKEN_CANARY", "secret")
    proc = supervisor.start_worker(
        [
            sys.executable,
            "-I",
            "-c",
            "import os,sys; sys.stdout.write(repr(sorted(os.environ)))",
        ]
    )
    out, _ = proc.communicate(timeout=60)
    assert b"CANARY" not in out and b"NX_" not in out


def test_the_real_worker_command_is_interpreter_dash_I_dash_m_module() -> None:
    argv = supervisor.worker_argv(supervisor.oslimits.limits_for(60))
    assert argv[:4] == [sys.executable, "-I", "-m", "nexus.lua_runtime.worker"]
    assert argv[4:] == [
        "--cpu-soft",
        "65",
        "--cpu-hard",
        "70",
        "--as-bytes",
        str(1 << 30),
    ]


def test_the_worker_reads_nothing_and_runs_nothing_until_the_first_frame() -> None:
    proc = supervisor.start_worker(
        supervisor.worker_argv(supervisor.oslimits.limits_for(60))
    )
    try:
        assert frames.read_frame(proc.stdout, frames.READY_FRAME_CAP) == {"t": "ready"}
        time.sleep(0.4)
        assert proc.poll() is None, "the worker left before the parent's first frame"
        proc.stdin.close()  # the parent goes away without a first frame: the worker sees end of file and exits 0
        assert proc.wait(timeout=20) == 0
        assert proc.stdout.read() == b"", (
            "the worker wrote something before the first frame"
        )
    finally:
        proc.kill()
        for s in (proc.stdin, proc.stdout, proc.stderr):
            s.close()


def test_the_start_frame_carries_grants_every_verb_name_and_the_limits() -> None:
    echo_back = ready_then(
        """
        send({"t": "result", "value": start, "stats": {"peak_lua_bytes": 7, "instructions": 9}})
        """
    )
    table = verb_table(
        verb("a.one", lambda x, deadline, ctx: x, "string"),
        verb("b.two", lambda x, deadline, ctx: x, "string"),
    )
    out = supervise(
        start("return 1", "a.one", name="n1"),
        limits=limits(30),
        verbs=table,
        config=WorkerConfig(argv=echo_back),
    )
    sent = out.value
    assert (
        sent["t"] == "start" and sent["name"] == "n1" and sent["source"] == "return 1"
    )
    assert sent["grants"] == ["a.one"]
    assert sent["verbs"] == [
        "a.one",
        "b.two",
    ]  # every verb in the table, granted or not
    assert sent["limits"] == {
        "instructions": 50_000_000,
        "memory": 64 * 1024 * 1024,
        "wall_time": 30.0,
        "result": 1024 * 1024,
        "depth": 32,
    }
    assert out.stats == {"peak_lua_bytes": 7, "instructions": 9}


def test_the_script_gets_its_args_as_one_table_through_the_main_chunk_vararg() -> None:
    src = 'local n = select("#", ...) local args = ... return {n = n, sum = args.a + args.b, s = args.name, absent = args.optional}'
    out = supervise(
        {**start(src), "args": {"a": 40, "b": 2, "name": "x"}},
        limits=limits(30),
        verbs=_TABLE,
        config=WorkerConfig(),
    )
    assert out.value == {
        "n": 1,
        "sum": 42,
        "s": "x",
    }  # one value; an absent optional is nil, so the key is absent


def test_a_start_with_no_args_sends_an_empty_table_not_null() -> None:
    echo_back = ready_then('send({"t": "result", "value": start["args"], "stats": {}})')
    out = supervise(
        start("return 1"),
        limits=limits(30),
        verbs=_TABLE,
        config=WorkerConfig(argv=echo_back),
    )
    assert out.value == {}


def test_a_nested_args_value_reaches_the_script_as_a_table() -> None:
    out = supervise(
        {
            **start("local a = ... return a.list[2] .. a.map.k"),
            "args": {"list": ["p", "q"], "map": {"k": "!"}},
        },
        limits=limits(30),
        verbs=_TABLE,
        config=WorkerConfig(),
    )
    assert out.value == "q!"


def test_limits_can_be_lowered_and_never_raised() -> None:
    lo = Limits().lowered(wall_time=5, memory=1 << 20)
    assert lo.wall_time == 5 and lo.memory == 1 << 20
    with pytest.raises(ValueError):
        Limits().lowered(wall_time=61)
    with pytest.raises(ValueError):
        Limits().lowered(memory=65 * 1024 * 1024)


# ── the real worker: results, errors, verbs ─────────────────────────────────


def test_a_one_line_fixture_returns_a_json_result_with_stats() -> None:
    out = _run("return 1 + 1")
    assert out.value == 2
    assert out.stats["peak_lua_bytes"] > 0


def test_a_verb_call_is_a_round_trip_and_its_result_returns_as_json() -> None:
    out = _run(
        'local r = fixture.echo("hi") return {got = r.got, n = #r.list, third = r.list[3]}'
    )
    assert out.value == {"got": "hi", "n": 3, "third": 3}


def test_a_lua_error_is_a_script_error_without_a_traceback() -> None:
    with pytest.raises(ScriptError) as err:
        _run('error("boom")')
    assert err.value.kind == "lua" and "boom" in err.value.detail
    assert "stack traceback" not in err.value.detail


def test_the_count_hook_reports_instructions_used() -> None:
    out = _run("local n = 0 for i = 1, 2000000 do n = n + i end return n")
    assert out.stats["instructions"] >= 1_000_000, out.stats


def test_a_result_over_the_result_bound_is_a_limit_not_a_worker_error() -> None:
    with pytest.raises(ScriptLimitError) as err:
        _run('return string.rep("x", 1100000)')
    assert err.value.limit == "result"
    assert (
        _run('return #string.rep("x", 900000)').value == 900000
    )  # 900 KB of source-side data is fine
    assert (
        len(_run('return string.rep("x", 900000)').value) == 900000
    )  # and a 900 KB result returns whole


def test_a_lua_memory_overrun_is_a_memory_limit_with_the_peak_reported() -> None:
    with pytest.raises(ScriptLimitError) as err:
        _run('return #string.rep("x", 1e9)')
    assert err.value.limit == "memory" and err.value.value == 64 * 1024 * 1024


# ── the parent's checks on every verb call ──────────────────────────────────

_CALL_AND_ECHO = """
send({"t": "ready"})
start = recv()
send({"t": "call", "id": 1, "verb": %(verb)r, "args": %(args)r})
reply = recv()
send({"t": "result", "value": reply})
"""


def _fake_call(
    verb_name: str, args: list[Any], grants: tuple[str, ...], table: Any = None
) -> Any:
    argv = fake_worker(_CALL_AND_ECHO % {"verb": verb_name, "args": args})
    return supervise(
        start("-", *grants),
        limits=limits(30),
        verbs=table or _TABLE,
        config=WorkerConfig(argv=argv),
    ).value


def test_an_ungranted_verb_sent_over_the_pipe_is_refused_by_name() -> None:
    table = verb_table(
        _ECHO,
        verb(
            "fixture.secret",
            lambda deadline, ctx: "leaked",
        ),
    )
    reply = _fake_call("fixture.secret", [], ("fixture.echo",), table)
    assert reply["ok"] is False and reply["error"]["kind"] == "ungranted"
    assert "fixture.secret" in reply["error"]["message"]


def test_a_verb_that_is_not_in_the_table_is_refused_even_when_named_in_the_grants() -> (
    None
):
    reply = _fake_call("fixture.nope", [], ("fixture.nope",))
    assert (
        reply["error"]["kind"] == "ungranted"
        and "fixture.nope" in reply["error"]["message"]
    )


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ([5], "must be a string"),
        ([[1, 2]], "must be a string"),
        ([{"a": 1}], "must be a string"),
        ([True], "must be a string"),
        ([], "takes 1 argument"),
        (["a", "b"], "takes 1 argument"),
        ([None], "required"),
    ],
    ids=["number", "list", "object", "bool", "too-few", "too-many", "nil-required"],
)
def test_a_verb_argument_of_the_wrong_type_or_count_is_refused_by_name(
    args: list[Any], needle: str
) -> None:
    reply = _fake_call("fixture.echo", args, ("fixture.echo",))
    assert reply["ok"] is False and reply["error"]["kind"] == "bad_args", reply
    assert (
        "fixture.echo" in reply["error"]["message"]
        and needle in reply["error"]["message"]
    ), reply


def test_the_real_worker_turns_a_refusal_into_a_lua_error_the_script_can_catch() -> (
    None
):
    out = _run("local ok, e = pcall(fixture.echo, 5) return {ok = ok, e = e}")
    assert out.value["ok"] is False and "fixture.echo" in out.value["e"]
    with pytest.raises(ScriptError) as err:
        _run("return fixture.echo(5)")
    assert err.value.kind == "lua" and "fixture.echo" in err.value.detail


def test_a_verb_failure_reaches_the_script_as_its_message_never_as_a_python_object() -> (
    None
):
    def boom(s: str, deadline: float, ctx: Any) -> Any:
        raise RuntimeError("it exploded")

    def refuses(s: str, deadline: float, ctx: Any) -> Any:
        raise VerbError("memory.get: no such project")

    table = verb_table(
        verb("fixture.boom", boom, "string"), verb("fixture.refuses", refuses, "string")
    )
    caught = _run(
        'local _, a = pcall(fixture.boom, "x") local _, b = pcall(fixture.refuses, "x") return {a = a, b = b}',
        verbs=table,
    ).value
    assert caught["a"] == "it exploded" and isinstance(caught["a"], str)
    assert caught["b"] == "memory.get: no such project"


def test_a_verb_result_over_the_frame_cap_is_a_script_visible_error_never_a_truncation() -> (
    None
):
    table = verb_table(verb("fixture.big", lambda deadline, ctx: "x" * 5000))
    out = _run(
        "local ok, e = pcall(fixture.big) return {ok = ok, e = e}",
        verbs=table,
        config=WorkerConfig(verb_frame_cap=1000),
    )
    assert out.value["ok"] is False and "over the 1000 cap" in out.value["e"]


def test_a_verb_result_that_does_not_fit_under_max_memory_is_a_memory_limit_not_a_worker_abort() -> (
    None
):
    """Gate r3 S3. 64 MB of Lua memory cannot hold ~2.6 MB of JSON empty tables (27 Lua bytes per JSON byte)."""
    table = verb_table(
        verb("fixture.empties", lambda deadline, ctx: [[] for _ in range(900_000)])
    )
    with pytest.raises(ScriptLimitError) as err:
        _run(
            "return #fixture.empties()",
            verbs=table,
            config=WorkerConfig(verb_frame_cap=8 * 1024 * 1024),
        )
    assert err.value.limit == "memory", err.value
    # the calling process is still serving
    assert _run("return 1").value == 1


def test_a_verb_result_with_an_integer_outside_64_bits_is_a_verb_error_the_script_can_catch() -> (
    None
):
    """lupa raises OverflowError on such an int; the parent refuses it before it reaches the worker."""
    table = verb_table(
        verb("fixture.wide", lambda deadline, ctx: {"n": 2**70}),
        verb("fixture.edge", lambda deadline, ctx: {"hi": 2**63 - 1, "lo": -(2**63)}),
    )
    out = _run(
        "local ok, e = pcall(fixture.wide) local edge = fixture.edge() return {ok = ok, e = e, hi = edge.hi}",
        verbs=table,
    )
    assert (
        out.value["ok"] is False
        and "64-bit" in out.value["e"]
        and "fixture.wide" in out.value["e"]
    )
    assert (
        out.value["hi"] == 2**63 - 1
    )  # the edge of the range still goes through, whole


def test_a_call_frame_with_an_integer_outside_64_bits_is_a_worker_error_not_an_overflow() -> (
    None
):
    err = _worker_error(
        'send({"t": "ready"}); recv(); raw(header(80) + b\'{"t":"call","id":1,"verb":"fixture.echo","args":[99999999999999999999]}\'.ljust(80)); time.sleep(30)'
    )
    assert "bad frame" in err.detail and "64-bit" in err.detail


# ── frame caps, bad frames, the call count ──────────────────────────────────


def _worker_error(body: str, **cfg: Any) -> ScriptWorkerError:
    with pytest.raises(ScriptWorkerError) as err:
        supervise(
            start("-", "fixture.echo"),
            limits=limits(30),
            verbs=_TABLE,
            config=WorkerConfig(argv=fake_worker(body), **cfg),
        )
    return err.value


def test_a_call_frame_over_64_kb_ends_the_run_with_a_worker_error() -> None:
    err = _worker_error(
        'send({"t": "ready"}); recv(); send({"t": "call", "id": 1, "verb": "fixture.echo", "args": ["a" * 70000]})'
    )
    assert "cap" in err.detail


def test_a_length_prefix_over_the_cap_is_refused_without_waiting_for_the_body() -> None:
    err = _worker_error(
        'send({"t": "ready"}); recv(); raw(header(0x7FFFFFFF)); time.sleep(30)'
    )
    assert "cap" in err.detail


def test_a_result_frame_over_1_mb_plus_64_kb_ends_the_run() -> None:
    err = _worker_error(
        'send({"t": "ready"}); recv(); raw(header(frames.END_FRAME_CAP + 1)); time.sleep(30)'
    )
    assert "cap" in err.detail


@pytest.mark.parametrize(
    "garbage",
    [
        "raw(header(5) + b'hello')",
        "raw(header(2) + b'[]')",
        "raw(header(7) + b'{\"a\":1}')",
        "raw(header(4) + b'\\xff\\xfe\\xfd\\xfc')",
    ],
    ids=["not-json", "array", "no-t", "bad-utf8"],
)
def test_a_malformed_frame_ends_the_run_with_a_worker_error(garbage: str) -> None:
    err = _worker_error('send({"t": "ready"}); recv(); ' + garbage + "; time.sleep(30)")
    assert "bad frame" in err.detail


def test_an_unknown_frame_type_is_a_worker_error() -> None:
    t0 = time.monotonic()
    err = _worker_error(
        'send({"t": "ready"}); recv(); send({"t": "sing"}); time.sleep(30)'
    )
    # the frame-type guard names the frame as unexpected at once; without it the type falls through to the
    # exit-status path, which waits out the 5 s exit grace and says "sent 'sing' and then exited"
    assert "unexpected" in err.detail and "sing" in err.detail, err.detail
    assert time.monotonic() - t0 < supervisor.EXIT_GRACE_S - 1
    if sys.platform != "win32":
        assert (
            err.signal == signal.SIGKILL and err.returncode is None
        )  # the parent ended it, it did not exit by itself


def test_the_first_frame_must_be_ready() -> None:
    err = _worker_error('send({"t": "result", "value": 1})')
    assert "ready" in err.detail


_CALLS = """
send({"t": "ready"}); recv()
for n in range(1, %(n)d + 1):
    send({"t": "call", "id": n, "verb": "fixture.echo", "args": ["x"]})
    r = recv()
    assert r["ok"], r
send({"t": "result", "value": "done"})
"""


def test_the_10000th_verb_call_is_served_and_the_10001st_ends_the_run() -> None:
    ok = supervise(
        start("-", "fixture.echo"),
        limits=limits(60),
        verbs=_TABLE,
        config=WorkerConfig(argv=fake_worker(_CALLS % {"n": 10_000})),
    )
    assert ok.value == "done"
    err = _worker_error(
        _CALLS % {"n": 10_001},
    )
    assert "10000 verb calls" in err.detail


# ── the worker-end table ────────────────────────────────────────────────────


def test_the_script_returned_is_a_result_frame_then_exit_0() -> None:
    out = supervise(
        start("-"),
        limits=limits(30),
        verbs=_TABLE,
        config=WorkerConfig(argv=ready_then('send({"t": "result", "value": [1, 2]})')),
    )
    assert out.value == [1, 2]


def test_a_result_frame_followed_by_a_bad_exit_is_a_worker_error() -> None:
    err = _worker_error(
        'send({"t": "ready"}); recv(); send({"t": "result", "value": 1}); sys.exit(9)'
    )
    assert err.returncode == 9


@pytest.mark.parametrize(
    ("frame", "kind"),
    [
        ('{"t": "error", "kind": "lua", "detail": "x:1: oops"}', "lua"),
        (
            '{"t": "error", "kind": "static_check", "detail": "global: print (line 3)"}',
            "static_check",
        ),
        (
            '{"t": "error", "kind": "result", "detail": "a function is not a result"}',
            "result",
        ),
    ],
)
def test_an_error_frame_is_a_script_error_of_its_kind(frame: str, kind: str) -> None:
    with pytest.raises(ScriptError) as err:
        supervise(
            start("-"),
            limits=limits(30),
            verbs=_TABLE,
            config=WorkerConfig(argv=ready_then(f"send({frame})")),
        )
    assert err.value.kind == kind and err.value.detail


@pytest.mark.parametrize(
    "name", ["instructions", "memory", "stack", "result", "wall_time"]
)
def test_a_limit_frame_is_a_script_limit_error_naming_the_limit_with_stats(
    name: str,
) -> None:
    body = f'send({{"t": "limit", "limit": "{name}", "value": 5, "stats": {{"peak_lua_bytes": 11, "instructions": 22}}}})'
    with pytest.raises(ScriptLimitError) as err:
        supervise(
            start("-"),
            limits=limits(30),
            verbs=_TABLE,
            config=WorkerConfig(argv=ready_then(body)),
        )
    assert err.value.limit == name and err.value.value == 5
    assert err.value.stats == {"peak_lua_bytes": 11, "instructions": 22}


# A runaway that only the outer limits can stop: one pattern match is one Lua
# instruction (A2, T2 nexus_rdr/242-research-29), so the runtime's count hook
# never fires during it, and with 2**30 backtracking paths it runs for minutes.
# A plain Lua loop is ended by the 50M-instruction limit in well under a
# second, before the kill timer, the CPU limit or a parent's death can.
_RUNAWAY = 'return ("a"):rep(30):find(("a?"):rep(30) .. ("a"):rep(30) .. "c")'


def test_the_kill_timer_ends_a_runaway_worker_at_the_wall_limit() -> None:
    t0 = time.monotonic()
    with pytest.raises(ScriptLimitError) as err:
        _run(_RUNAWAY, wall=1)
    assert err.value.limit == "wall_time" and err.value.value == 1
    assert 0.9 <= time.monotonic() - t0 < 6


@pytest.mark.parametrize(
    "source",
    [
        'return ("a"):rep(200):find(("a*"):rep(6) .. "b")',
        'return ("a"):rep(30):find(("a?"):rep(30) .. ("a"):rep(30) .. "c")',
        # single C calls that allocate nothing and that the count hook never sees (nexus-mdbds.5 review r1)
        'return string.rep("", 2^40)',
        "return table.move({}, 1, 2^40, 1)",
    ],
    ids=["star-chain", "optional-chain", "empty-rep", "table-move"],
)
def test_a_pathological_pattern_is_killed_at_the_wall_limit_and_the_caller_keeps_serving(
    source: str,
) -> None:
    """A2 (T2 nexus_rdr/242-research-29): one pattern match is one instruction; only the kill timer stops it."""
    t0 = time.monotonic()
    with pytest.raises(ScriptLimitError) as err:
        _run(source, wall=2)
    assert err.value.limit == "wall_time" and time.monotonic() - t0 < 8
    assert _run("return 7").value == 7


def test_a_worker_killed_by_the_timer_during_a_verb_is_a_wall_time_limit() -> None:
    gate = Gate()
    table = verb_table(verb("fixture.hang", gate))
    t0 = time.monotonic()
    with pytest.raises(ScriptLimitError) as err:
        _run("return fixture.hang()", wall=1, verbs=table)
    gate.opened.set()
    assert err.value.limit == "wall_time" and time.monotonic() - t0 < 6


def test_eof_with_no_result_frame_is_a_worker_error_with_the_exit_status_and_stderr() -> (
    None
):
    err = _worker_error(
        'send({"t": "ready"}); recv(); sys.stderr.write("worker said boom\\n"); sys.exit(0)'
    )
    assert err.returncode == 0 and "worker said boom" in err.stderr_head
    assert "without a result" in err.detail


def test_stderr_keeps_only_the_first_64_kb() -> None:
    err = _worker_error(
        'send({"t": "ready"}); recv(); sys.stderr.write("A" * 70000 + "B" * 70000); sys.stderr.flush(); sys.exit(1)'
    )
    assert len(err.stderr_head) == 64 * 1024 and set(err.stderr_head) == {"A"}


def test_another_exit_code_is_a_worker_error_carrying_it() -> None:
    err = _worker_error('send({"t": "ready"}); recv(); sys.exit(17)')
    assert err.returncode == 17 and err.signal is None


def test_exit_code_3_is_the_os_memory_limit() -> None:
    with pytest.raises(ScriptLimitError) as err:
        supervise(
            start("-"),
            limits=limits(30),
            verbs=_TABLE,
            config=WorkerConfig(argv=ready_then("sys.exit(3)")),
        )
    assert err.value.limit == "memory" and err.value.value == 1 << 30


def test_a_worker_that_cannot_set_its_os_limits_ends_the_run_before_reading_the_source() -> (
    None
):
    argv = (sys.executable, "-I", "-m", "nexus.lua_runtime.worker", "--cpu-soft", "x")
    with pytest.raises(ScriptWorkerError) as err:
        supervise(
            start("-"), limits=limits(30), verbs=_TABLE, config=WorkerConfig(argv=argv)
        )
    assert err.value.returncode == 4 and "cannot set OS limits" in err.value.stderr_head


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_a_sigsegv_the_test_sends_is_a_worker_error_naming_the_signal() -> None:
    gate = Gate()
    pids: list[int] = []
    table = verb_table(verb("fixture.hang", gate))

    def send_segv() -> None:
        assert gate.entered.wait(30)
        os.kill(pids[0], signal.SIGSEGV)

    threading.Thread(target=send_segv, daemon=True).start()
    with pytest.raises(ScriptWorkerError) as err:
        supervise(
            start("return fixture.hang()", "fixture.hang"),
            limits=limits(30),
            verbs=table,
            config=WorkerConfig(on_spawn=lambda p: pids.append(p.pid)),
        )
    gate.opened.set()
    assert err.value.signal == signal.SIGSEGV, err.value.detail


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigxcpu_from_the_cpu_limit_is_a_cpu_time_limit_when_the_timer_has_not_fired() -> (
    None
):
    # wall 6 s, CPU soft 2 s (margin -4): the OS limit goes first
    cfg = WorkerConfig(cpu_margins=(-4, -3))
    t0 = time.monotonic()
    with pytest.raises(ScriptLimitError) as err:
        _run(_RUNAWAY, wall=6, config=cfg)
    assert err.value.limit == "cpu_time" and err.value.value == 2, err.value
    assert time.monotonic() - t0 < 6


@platform_only("win32")
def test_windows_job_time_limit_is_reported_as_cpu_time() -> None:
    # The job enforces a per-process time limit late: a 2 s limit ended a spinner at 7 s when measured
    # (test_win_job_limits). So the limit is 3 s here and the wall limit 20 s, well past the lag.
    cfg = WorkerConfig(cpu_margins=(-17, -16))
    with pytest.raises(ScriptLimitError) as err:
        _run(_RUNAWAY, wall=20, config=cfg)
    assert err.value.limit == "cpu_time" and err.value.value == 3, err.value


_OS_MEMORY = 300 * 1024 * 1024


def _memory_error_run() -> ScriptLimitError:
    """A ~19 MB verb result of empty lists: json.loads in the worker needs ~400 MB, over the OS limit."""
    # the same limit must let an ordinary run through, or the test proves only that the worker cannot start
    assert _run("return 1", config=WorkerConfig(os_memory_bytes=_OS_MEMORY)).value == 1
    table = verb_table(verb("fixture.big", lambda deadline, ctx: [[]] * 6_500_000))
    with pytest.raises(ScriptLimitError) as err:
        _run(
            "return #fixture.big()",
            verbs=table,
            wall=60,
            config=WorkerConfig(os_memory_bytes=_OS_MEMORY),
        )
    # the OS limit's value, not the 64 MiB Lua max_memory: a build that overran max_memory would also be "memory"
    assert err.value.value == _OS_MEMORY, (
        f"memory limit came from {err.value.value}, not the OS limit"
    )
    return err.value


@platform_only("linux")
def test_linux_rlimit_as_makes_the_worker_exit_3_and_the_run_a_memory_limit() -> None:
    assert _memory_error_run().limit == "memory"


@platform_only("win32")
def test_windows_job_memory_limit_makes_the_worker_exit_3_and_the_run_a_memory_limit() -> (
    None
):
    assert _memory_error_run().limit == "memory"


# ── budget: remaining wall time reaches the verb, a verb that ignores it cannot hold the run ──


def test_each_verb_gets_the_remaining_budget_as_its_deadline() -> None:
    seen: list[float] = []

    def nap(s: float, deadline: float, ctx: Any) -> Any:
        time.sleep(s)
        return 0

    def probe(deadline: float, ctx: Any) -> Any:
        seen.append(deadline - time.monotonic())
        return 0

    table = verb_table(verb("fixture.nap", nap, "number"), verb("fixture.probe", probe))
    _run(
        "fixture.probe() fixture.nap(1.0) fixture.probe() return 1",
        wall=30,
        verbs=table,
    )
    first, second = seen
    assert 25 < first <= 30, seen
    assert second < first - 0.9, seen  # the second call sees the budget less the nap


def test_a_verb_that_ignores_its_deadline_cannot_hold_the_run_past_the_limit() -> None:
    """Gate r3 S2: a verb with no timeout to hand over; the run loop stops waiting for it."""
    release = threading.Event()
    finished = threading.Event()

    def stubborn(
        deadline: float, ctx: Any
    ) -> Any:  # sleeps 5 s whatever the deadline says
        release.wait(5)
        finished.set()
        return "late result"

    table = verb_table(verb("fixture.stubborn", stubborn))
    t0 = time.monotonic()
    with pytest.raises(ScriptLimitError) as err:
        _run("return fixture.stubborn()", wall=1, verbs=table)
    took = time.monotonic() - t0
    assert err.value.limit == "wall_time"
    assert took < 3.5, (
        f"supervise waited {took:.1f}s for a verb that ignores its timeout"
    )
    assert not finished.is_set(), (
        "the verb should still be running when supervise returned"
    )
    assert _run("return 5").value == 5  # nothing left behind blocks the next run
    release.set()
    assert finished.wait(10)  # the abandoned verb ends alone; its result goes nowhere


def test_a_verb_that_raises_verbtimeout_ends_the_run_on_wall_time_and_the_worker_sees_no_reply() -> (
    None
):
    done = threading.Event()

    def polite(deadline: float, ctx: Any) -> Any:
        while time.monotonic() < deadline:
            time.sleep(0.02)
        done.set()
        raise VerbTimeout("fixture.polite: deadline passed")

    table = verb_table(verb("fixture.polite", polite))
    with pytest.raises(ScriptLimitError) as err:
        _run(
            "local ok = pcall(fixture.polite) return 'the script survived a timeout'",
            wall=1,
            verbs=table,
        )
    assert (
        err.value.limit == "wall_time"
    )  # a pcall in the script cannot swallow it: it never sees a reply
    assert done.wait(5), "a verb that checks its deadline stops at it"


def test_a_verbtimeout_before_the_deadline_still_ends_the_run_on_wall_time() -> None:
    def impatient(deadline: float, ctx: Any) -> Any:
        raise VerbTimeout("fixture.impatient: gave up early")

    table = verb_table(verb("fixture.impatient", impatient))
    t0 = time.monotonic()
    with pytest.raises(ScriptLimitError) as err:
        _run("return fixture.impatient()", wall=30, verbs=table)
    assert err.value.limit == "wall_time" and time.monotonic() - t0 < 10


# ── the cap of four workers ─────────────────────────────────────────────────


def test_a_fifth_concurrent_run_fails_at_once_with_script_busy_and_a_slot_frees_when_one_ends() -> (
    None
):
    gate = Gate()
    entered = threading.Semaphore(0)

    def hold(deadline: float, ctx: Any) -> Any:
        entered.release()
        gate.opened.wait(60)
        return 1

    table = verb_table(verb("fixture.hold", hold))
    results: list[Any] = []

    def runner() -> None:
        try:
            results.append(_run("return fixture.hold()", verbs=table).value)
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(frames.MAX_WORKERS)]
    for t in threads:
        t.start()
    for _ in threads:
        assert entered.acquire(timeout=30), "a run never reached its verb"
    t0 = time.monotonic()
    with pytest.raises(ScriptBusyError):
        _run("return 1")
    assert time.monotonic() - t0 < 1, "a busy refusal must not wait"
    gate.opened.set()
    for t in threads:
        t.join(30)
    assert results == [1] * frames.MAX_WORKERS, results
    assert _run("return 2").value == 2  # the slots are back


def test_the_cap_is_four() -> None:
    assert frames.MAX_WORKERS == 4


# ── parent death ────────────────────────────────────────────────────────────

_PARENT = textwrap.dedent(
    """
    import sys, time
    from nexus.lua_runtime.supervisor import supervise, Limits, WorkerConfig
    from nexus.lua_runtime.verb_types import VerbSpec
    mode = sys.argv[1]
    def hang(deadline, ctx):
        time.sleep(600)
    table = {"fixture.hang": VerbSpec("fixture.hang", (), hang)}
    src = sys.argv[2] if mode == "spin" else "return fixture.hang()"  # _RUNAWAY, passed in argv
    cfg = WorkerConfig(cpu_margins=(-27, -26), on_spawn=lambda p: (print(p.pid, flush=True)))
    grants = ["fixture.hang"] if mode != "spin" else []  # the static check refuses a grant the script does not use
    supervise({"name": "n", "source": src, "grants": grants},
              limits=Limits().lowered(wall_time=30), verbs=table, config=cfg)
    """
)


def _pid_gone(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
        ).stdout
        return str(pid) not in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # a zombie answers kill(0): reparented orphans are reaped by init, so poll state
    ps = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return ps == "" or ps.startswith("Z")


def _kill_parent_and_wait_for_worker(mode: str, within: float) -> float:
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, mode, _RUNAWAY],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        worker_pid = int(parent.stdout.readline())
        time.sleep(1.0 if mode == "spin" else 0.5)
        assert not _pid_gone(worker_pid)
        parent.kill()
        parent.wait(30)
        t0 = time.monotonic()
        while time.monotonic() - t0 < within:
            if _pid_gone(worker_pid):
                return time.monotonic() - t0
            time.sleep(0.1)
        os.kill(
            worker_pid, signal.SIGKILL
        ) if sys.platform != "win32" else subprocess.run(
            ["taskkill", "/F", "/PID", str(worker_pid)], capture_output=True
        )
        pytest.fail(
            f"the worker (pid {worker_pid}) outlived its parent by more than {within}s in mode {mode}"
        )
    finally:
        parent.kill()
        parent.stdout.close()


@platform_only("linux")
def test_linux_a_worker_whose_parent_is_killed_ends_within_its_cpu_limit() -> None:
    assert _kill_parent_and_wait_for_worker("spin", within=10) < 10


@platform_only("darwin")
def test_macos_a_worker_whose_parent_is_killed_ends_within_its_cpu_limit() -> None:
    assert _kill_parent_and_wait_for_worker("spin", within=10) < 10


@platform_only("linux")
def test_linux_a_worker_waiting_on_the_pipe_reads_end_of_file_when_the_parent_dies() -> (
    None
):
    assert _kill_parent_and_wait_for_worker("wait", within=5) < 5


@platform_only("darwin")
def test_macos_a_worker_waiting_on_the_pipe_reads_end_of_file_when_the_parent_dies() -> (
    None
):
    assert _kill_parent_and_wait_for_worker("wait", within=5) < 5


@platform_only("win32")
def test_windows_a_worker_dies_at_once_when_its_parent_dies_by_the_job_kill_on_close() -> (
    None
):
    assert _kill_parent_and_wait_for_worker("spin", within=5) < 5
    assert _kill_parent_and_wait_for_worker("wait", within=5) < 5
