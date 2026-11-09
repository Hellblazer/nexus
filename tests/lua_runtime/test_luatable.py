# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 gate r3 S3: a verb result is built into a Lua table on a path where an overrun is an error, not an abort.

``LuaRuntime.table_from`` calls the Lua C API outside any protected call, so
a table that does not fit under ``max_memory`` aborts the whole process
(``PANIC: unprotected error in call to Lua API``). ``luatable.LuaTableBuilder``
hands a subtree to ``table_from`` only when its worst-case size fits, and
builds the rest through protected Lua calls, so the overrun is
``LuaMemoryError``. Each check that could abort the interpreter runs in a
child process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from typing import Any

import lupa.lua54 as lua
import pytest

from nexus.lua_runtime import luatable, verb_types

_SHAPES = {
    "rows": "[{'id': i, 'title': 'session %d title' % i, 'project': 'nexus', 'ts': 1700000000 + i, 'flag': True, 'tags': ['a', 'b', 'c']} for i in range(n)]",
    "empty_tables": "[[] for _ in range(n)]",
    "empty_objects": "[{} for _ in range(n)]",
    "zeros": "[0] * n",
    "short_strings": "[str(i) for i in range(n)]",
    "singleton_objects": "[{'a': i} for i in range(n)]",
    "nested": "[[[i]] for i in range(n)]",
    "dict_of_ints": "{str(i): i for i in range(n)}",
    "long_strings": "['x' * 1000 for _ in range(n // 100)]",
    # chains of single-element arrays: the densest shape found, 36 Lua bytes per JSON byte at any depth (r1, N1)
    "deep_chain_3": "[chain(3) for _ in range(n // 6)]",
    "deep_chain_32": "[chain(32) for _ in range(n // 64)]",
    "deep_chain_300": "[chain(300) for _ in range(n // 600)]",
}

_CHILD = textwrap.dedent(
    """
    import json, sys
    import lupa.lua54 as lua
    from nexus.lua_runtime.luatable import LuaTableBuilder
    shape, json_bytes, max_memory, mode = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    SHAPES = %(shapes)r
    def chain(d):
        v = []
        for _ in range(d - 1):
            v = [v]
        return v
    n = 1000
    probe = len(json.dumps(eval(SHAPES[shape]), separators=(",", ":")))
    n = max(10, int(1000 * json_bytes / probe))
    value = eval(SHAPES[shape])
    size = len(json.dumps(value, separators=(",", ":")))
    rt = lua.LuaRuntime(register_eval=False, register_builtins=False, unpack_returned_tuples=False, max_memory=max_memory)
    base = rt.get_memory_used()
    try:
        if mode == "naive":
            rt.table_from(value, recursive=True)
        else:
            LuaTableBuilder(rt).build(value, size)
    except lua.LuaMemoryError:
        print(json.dumps({"outcome": "memory", "json": size}))
        sys.exit(0)
    print(json.dumps({"outcome": "built", "json": size, "used": rt.get_memory_used() - base}))
    """
) % {"shapes": _SHAPES}


def _child(
    shape: str, json_bytes: int, max_memory: int, mode: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _CHILD,
            shape,
            str(json_bytes),
            str(max_memory),
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_control_table_from_over_max_memory_aborts_the_process() -> None:
    """The premise of S3. When lupa makes table_from protected this goes red: the builder is then unnecessary."""
    proc = _child("empty_tables", 2_000_000, 16 * 1024 * 1024, "naive")
    assert proc.returncode != 0, f"table_from survived an overrun: {proc.stdout}"
    assert "PANIC" in proc.stderr and "not enough memory" in proc.stderr, proc.stderr[
        -400:
    ]


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_the_builder_turns_an_overrun_into_a_memory_error_for_every_shape(
    shape: str,
) -> None:
    # 16 MB of Lua memory against ~2 MB of JSON: some shapes fit (long strings), most do not
    proc = _child(shape, 2_000_000, 16 * 1024 * 1024, "protected")
    assert proc.returncode == 0, (
        f"{shape}: the worker aborted ({proc.returncode}): {proc.stderr[-400:]}"
    )
    assert json.loads(proc.stdout)["outcome"] in ("memory", "built")


def test_the_worst_shape_really_overruns_and_a_light_shape_really_fits() -> None:
    worst = json.loads(
        _child("empty_tables", 2_000_000, 16 * 1024 * 1024, "protected").stdout
    )
    light = json.loads(
        _child("long_strings", 2_000_000, 16 * 1024 * 1024, "protected").stdout
    )
    assert worst["outcome"] == "memory" and light["outcome"] == "built"


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_no_shape_measured_costs_more_than_the_worst_case_ratio(shape: str) -> None:
    """The constant the builder relies on: Lua bytes per JSON byte, from the built table's own count."""
    proc = _child(shape, 2_000_000, 1 << 30, "protected")
    assert proc.returncode == 0, proc.stderr[-400:]
    got = json.loads(proc.stdout)
    assert got["outcome"] == "built"
    ratio = got["used"] / got["json"]
    assert ratio <= luatable.WORST_BYTES_PER_JSON_BYTE, (
        f"{shape}: {ratio:.1f} Lua bytes per JSON byte"
    )


def test_a_result_that_fits_is_one_table_from_and_one_that_does_not_is_built_in_pieces() -> (
    None
):
    class Counting:
        def __init__(self, rt: Any) -> None:
            self._rt = rt
            self.table_from_calls = 0

        def table_from(self, *a: Any, **k: Any) -> Any:
            self.table_from_calls += 1
            return self._rt.table_from(*a, **k)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._rt, name)

    rows = [{"id": i, "tags": ["a", "b"]} for i in range(2000)]
    size = len(json.dumps(rows, separators=(",", ":")))
    big = Counting(
        lua.LuaRuntime(
            register_eval=False, register_builtins=False, max_memory=64 * 1024 * 1024
        )
    )
    luatable.LuaTableBuilder(big).build(rows, size)
    assert (
        big.table_from_calls == 1
    )  # far from the limit: the whole result is one C-speed build
    tight = Counting(
        lua.LuaRuntime(
            register_eval=False, register_builtins=False, max_memory=size * 20
        )
    )
    table = luatable.LuaTableBuilder(tight).build(rows, size)
    assert (
        tight.table_from_calls > 1
    )  # near the limit: split until each piece cannot overrun
    assert len(table) == 2000 and table[2000]["id"] == 1999


def test_the_verb_result_cap_is_the_measured_20_mib() -> None:
    """Default loop. The slow fit test (test_cost_measurement) re-measures this against the ratio; this pins the value."""
    assert verb_types.VERB_RESULT_CAP_BYTES == 20 * 1024 * 1024, (
        "VERB_RESULT_CAP_BYTES changed: re-run `pytest -m slow tests/lua_runtime/test_cost_measurement.py -s` on "
        "every platform and update nexus_rdr/242-research-36 and RDR-240's A2 bound with it"
    )
