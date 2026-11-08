# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 2: what the worker costs, and what a verb result may weigh (opt-in: ``-m slow``).

Run on each platform::

    NX_TEST_T2_SUBSTRATE=none uv run pytest -m slow tests/lua_runtime/test_cost_measurement.py -s -p no:cacheprovider

and paste the ``MEASURE`` lines into the bead. A missed line is a STOP: it
goes back to RDR-242 before Step 3 (nexus-mdbds.5) starts. Nothing here works
around a miss.

Timing pass lines (RDR-242 Implementation Plan, Phase 1 Step 2):

* the median of 20 worker starts, spawn to the worker's first ready frame
  with ``lupa`` imported: at most 500 ms;
* a 1 MB verb-result round trip (the parent encodes it, the worker decodes it
  into a Lua table): at most 100 ms;
* a 50 MB round trip: at most 5 s. A 50 MB result cannot ship (the cap is
  below it, and 64 MB of Lua memory cannot hold it), so this line is measured
  with the cap and the memory limits lifted for the measurement only: it times
  the transport, the decode and the build, not a run a caller can make.

The fit line (gate r3 S3), which replaces "records whether the 50 MB table
fits": the verb-result cap, the largest JSON the parent will send, must hold
a result of typical records inside ``max_memory`` with a quarter of it left
for the script's own work, so ``cap <= 0.75 * 64 MiB / r`` where ``r`` is the
largest measured Lua-bytes-per-JSON-byte of the typical record shapes below.
PASS LINE: that bound is at least 8 MiB (RDR-240's A2 needs a day's records
under half the cap; a day four times the 30-day average of about 0.7 MB is
under 3 MB). CONSEQUENCE of a miss: RDR-240's queries move to one-hour
windows, its own stated remedy, RDR-242's text takes the measured cap, and
Step 3 does not start until it does. The cap is
``verb_types.VERB_RESULT_CAP_BYTES``; the last test fails when that constant
exceeds the bound, and names the number to set.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import replace
from typing import Any

import lupa.lua54 as lua
import pytest

from nexus.lua_runtime import frames, oslimits, supervisor, verb_types
from nexus.lua_runtime.luatable import LuaTableBuilder
from nexus.lua_runtime.limits import Limits
from nexus.lua_runtime.supervisor import WorkerConfig, supervise
from tests.lua_runtime._support import start, verb, verb_table

pytestmark = pytest.mark.slow

_MIB = 1024 * 1024
START_PASS_S = 0.5
ROUND_1MB_PASS_S = 0.1
ROUND_50MB_PASS_S = 5.0
FIT_PASS_LINE_BYTES = 8 * _MIB
#: Fraction of max_memory a result may take; the rest is the script's own work.
FIT_FRACTION = 0.75


def _session(i: int, bodies: bool) -> dict[str, Any]:
    """A session record of the kind RDR-240's A2 describes (metadata, tool counts, a prompt; bodies optional)."""
    rec: dict[str, Any] = {
        "session_id": f"{i:08x}-1234-4abc-9def-{i * 7919 % 10**12:012d}",
        "project": "nexus" if i % 3 else "conexus-plugin",
        "cwd": f"/Users/someone/git/nexus-wt/session-{i % 40}",
        "started_at": f"2026-10-{1 + i % 28:02d}T{i % 24:02d}:{i % 60:02d}:11Z",
        "ended_at": f"2026-10-{1 + i % 28:02d}T{(i + 1) % 24:02d}:{i % 60:02d}:52Z",
        "model": "claude-opus-4-1",
        "message_count": 40 + i % 300,
        "tool_calls": {
            "Bash": 12 + i % 9,
            "Read": 30 + i % 11,
            "Edit": 5 + i % 4,
            "Grep": i % 17,
            "Agent": i % 5,
            "Write": i % 3,
        },
        "deferred_tools": ["WebFetch", "NotebookEdit"][: i % 3],
        "first_prompt": f"Implement bead nexus-{i:05d}: the worker process, test first, and report the red runs for each test "
        * 1,
        "truncated": False,
    }
    if bodies:
        rec["assistant_text"] = [
            f"Step {k}: read the module, wrote the failing test, then made it pass. "
            * 6
            for k in range(3)
        ]
    return rec


def _records(shape: str, target_bytes: int) -> list[Any]:
    if shape == "sessions":
        make = lambda i: _session(i, False)  # noqa: E731
    elif (
        shape == "sessions_lean"
    ):  # the same records with no prompt text: the densest a session list gets
        make = lambda i: {
            k: v
            for k, v in _session(i, False).items()
            if k not in ("first_prompt", "cwd", "model")
        }  # noqa: E731
    elif shape == "sessions_with_bodies":
        make = lambda i: _session(i, True)  # noqa: E731
    elif shape == "memory_entries":
        make = lambda i: {
            "project": "nexus",
            "title": f"entry-{i}",
            "content": "a note about the design " * 12,
            "tags": "a,b",
            "ttl": None,
        }  # noqa: E731
    else:  # "diff_stat"
        make = lambda i: {
            "path": f"src/nexus/module_{i}.py",
            "added": i % 90,
            "removed": i % 40,
        }  # noqa: E731
    one = (
        len(
            json.dumps(
                [make(k) for k in range(200)], separators=(",", ":"), ensure_ascii=False
            )
        )
        / 200
    )
    return [make(i) for i in range(max(1, int(target_bytes / one)))]


#: The verbs whose results can reach megabytes (transcripts.sessions, memory.list/search, tables.get). The cap is
#: sized from their record shapes. ``diff_stat`` (git.diff_stat/diff_names: a few KB of short paths and counts) is
#: measured and printed but does not size the cap: it never gets near it, and a result of any shape that still
#: does not fit is a memory limit, not an abort (luatable).
_LARGE_VERBS = ("sessions", "sessions_lean", "sessions_with_bodies", "memory_entries")
_TYPICAL = (*_LARGE_VERBS, "diff_stat")


def _json_len(value: Any) -> int:
    return len(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def _lua_bytes(value: Any, max_memory: int = 1 << 31) -> int:
    rt = lua.LuaRuntime(
        register_eval=False,
        register_builtins=False,
        unpack_returned_tuples=False,
        max_memory=max_memory,
    )
    base = rt.get_memory_used()
    LuaTableBuilder(rt).build(value)
    return rt.get_memory_used() - base


def _line(**kv: Any) -> None:
    sys.stdout.write(
        "MEASURE platform=%s " % sys.platform
        + " ".join(f"{k}={v}" for k, v in kv.items())
        + "\n"
    )
    sys.stdout.flush()


def test_worker_start_median_of_20_is_at_most_500_ms() -> None:
    argv = supervisor.worker_argv(oslimits.limits_for(60))
    supervisor.start_worker(argv).kill()  # warm the file cache once; not counted
    times: list[float] = []
    for _ in range(20):
        t0 = time.perf_counter()
        proc = supervisor.start_worker(argv)
        try:
            ready = frames.read_frame(proc.stdout, frames.READY_FRAME_CAP)
            times.append(time.perf_counter() - t0)
            assert ready == {"t": "ready"}
        finally:
            proc.kill()
            for s in (proc.stdin, proc.stdout, proc.stderr):
                s.close()
            proc.wait()
    median = statistics.median(times)
    _line(
        metric="worker_start_median_ms",
        value=round(median * 1000, 1),
        min=round(min(times) * 1000, 1),
        max=round(max(times) * 1000, 1),
        pass_ms=int(START_PASS_S * 1000),
    )
    assert median <= START_PASS_S, (
        f"worker start median {median * 1000:.0f} ms is over {START_PASS_S * 1000:.0f} ms"
    )


def _round_trip(
    result: Any, *, config: WorkerConfig, limits: Limits, repeat: int = 1
) -> list[float]:
    """Seconds from the parent receiving the big call to receiving the next call: encode, send, decode, build."""
    marks: dict[str, float] = {}

    def big(deadline: float, ctx: Any) -> Any:
        marks["big"] = time.perf_counter()
        return result

    def mark(deadline: float, ctx: Any) -> Any:
        marks["mark"] = time.perf_counter()
        return 0

    table = verb_table(verb("fixture.big", big), verb("fixture.mark", mark))
    out: list[float] = []
    for _ in range(repeat):
        supervise(
            start(
                "local t = fixture.big() fixture.mark() return #t",
                "fixture.big",
                "fixture.mark",
            ),
            limits=limits,
            verbs=table,
            config=config,
        )
        out.append(marks["mark"] - marks["big"])
    return out


def test_one_megabyte_verb_result_round_trip_is_at_most_100_ms() -> None:
    rows = _records("sessions", 1_000_000)
    size = _json_len(rows)
    times = _round_trip(
        rows, config=WorkerConfig(), limits=Limits().lowered(wall_time=60), repeat=7
    )
    median = statistics.median(times)
    _line(
        metric="round_trip_1mb_ms",
        json_bytes=size,
        value=round(median * 1000, 1),
        min=round(min(times) * 1000, 1),
        pass_ms=int(ROUND_1MB_PASS_S * 1000),
    )
    assert median <= ROUND_1MB_PASS_S, (
        f"1 MB round trip {median * 1000:.0f} ms is over {ROUND_1MB_PASS_S * 1000:.0f} ms"
    )


def test_fifty_megabyte_verb_result_round_trip_is_at_most_5_s_with_the_caps_lifted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _records("sessions", 50_000_000)
    size = _json_len(rows)
    lifted = 256 * _MIB
    monkeypatch.setattr(
        verb_types, "VERB_RESULT_CAP_BYTES", 100 * _MIB
    )  # encode_result reads it at call time
    config = WorkerConfig(verb_frame_cap=100 * _MIB, os_memory_bytes=4 << 30)
    limits = replace(Limits(), wall_time=120.0, memory=1 << 30, result=lifted)
    times = _round_trip(rows, config=config, limits=limits, repeat=2)
    best = min(times)
    _line(
        metric="round_trip_50mb_s",
        json_bytes=size,
        value=round(best, 2),
        runs=[round(t, 2) for t in times],
        pass_s=ROUND_50MB_PASS_S,
        caps_lifted="verb cap 100 MiB, max_memory 1 GiB, address space 4 GiB",
    )
    assert best <= ROUND_50MB_PASS_S, (
        f"50 MB round trip {best:.2f} s is over {ROUND_50MB_PASS_S} s"
    )


def _ratios() -> dict[str, float]:
    out = {}
    for shape in _TYPICAL:
        rows = _records(shape, 4_000_000)
        out[shape] = _lua_bytes(rows) / _json_len(rows)
    return out


def test_the_json_to_lua_ratio_and_the_verb_result_bound() -> None:
    ratios = _ratios()
    worst_typical = max(ratios[k] for k in _LARGE_VERBS)
    max_memory = Limits().memory
    bound = int(FIT_FRACTION * max_memory / worst_typical)
    bound_mib = bound // _MIB
    empties = [[] for _ in range(100_000)]
    _line(
        metric="json_to_lua_ratio",
        **{k: round(v, 2) for k, v in ratios.items()},
        worst_shape_empty_tables=round(_lua_bytes(empties) / _json_len(empties), 2),
        worst_typical=round(worst_typical, 2),
        bound_mib=bound_mib,
        pass_line_mib=FIT_PASS_LINE_BYTES // _MIB,
        cap_constant_mib=round(verb_types.VERB_RESULT_CAP_BYTES / _MIB, 2),
    )
    assert bound >= FIT_PASS_LINE_BYTES, (
        f"the fit line missed: at {worst_typical:.2f} Lua bytes per JSON byte a result may weigh {bound_mib} MiB of JSON, "
        f"under the {FIT_PASS_LINE_BYTES // _MIB} MiB pass line. Consequence: RDR-240's queries move to one-hour windows, "
        "RDR-242 takes the measured cap, and Step 3 waits."
    )


def test_a_typical_result_at_the_verb_cap_fits_under_max_memory_in_a_real_run() -> None:
    """The cap constant is consistent with the measured ratio: a typical result of exactly that weight runs."""
    ratios = _ratios()
    worst = max(ratios[k] for k in _LARGE_VERBS)
    bound = int(FIT_FRACTION * Limits().memory / worst)
    cap = verb_types.VERB_RESULT_CAP_BYTES
    assert cap <= bound, (
        f"VERB_RESULT_CAP_BYTES is {cap} ({cap / _MIB:.1f} MiB) but typical records hold at most {bound / _MIB:.1f} MiB "
        f"inside {FIT_FRACTION:.0%} of max_memory at {worst:.2f} Lua bytes per JSON byte: set it to {bound // _MIB} MiB"
    )
    rows = _records("sessions", int(cap * 0.95))
    took = _round_trip(
        rows, config=WorkerConfig(), limits=Limits().lowered(wall_time=60)
    )[0]
    _line(
        metric="cap_sized_round_trip_s",
        json_bytes=_json_len(rows),
        value=round(took, 2),
    )
