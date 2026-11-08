# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 shared seams (T2 nexus/rdr242-interfaces, section A).

The verb types, the run errors and the verb table that the worker
(nexus-mdbds.4), the memory and tables verbs (nexus-mdbds.7) and the git and
transcripts verbs (nexus-mdbds.8) all build on.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from nexus.lua_runtime import errors, verb_types, verbs
from nexus.lua_runtime.verb_types import ParamSpec, VerbError, VerbSpec, check_args


def _fn(*args: object, deadline: float) -> object:
    return list(args)


_GET = VerbSpec(
    "memory.get",
    (ParamSpec("project", "string"), ParamSpec("title", "string")),
    _fn,
)
_SEARCH = VerbSpec(
    "memory.search",
    (
        ParamSpec("query", "string"),
        ParamSpec("project", "string"),
        ParamSpec("limit", "integer", optional=True),
    ),
    _fn,
)


def test_check_args_accepts_the_declared_types() -> None:
    check_args(_GET, ["p", "t"])
    check_args(_SEARCH, ["q", "p", 5])
    check_args(_SEARCH, ["q", "p", None])
    check_args(_SEARCH, ["q", "p"])  # a trailing optional may be omitted


@pytest.mark.parametrize(
    ("spec", "args", "needle"),
    [
        (_GET, ["p"], "takes 2"),
        (_GET, ["p", "t", "x"], "takes 2"),
        (_GET, ["p", 3], "title"),
        (_GET, [None, "t"], "project"),
        (_SEARCH, ["q", "p", 2.5], "limit"),
        (_SEARCH, ["q", "p", True], "limit"),  # a bool is not an integer
        (_SEARCH, ["q", "p", "5"], "limit"),
    ],
)
def test_check_args_refuses_by_parameter_name(
    spec: VerbSpec, args: list[object], needle: str
) -> None:
    with pytest.raises(VerbError, match=needle):
        check_args(spec, args)


def test_number_accepts_int_and_float_but_not_bool() -> None:
    spec = VerbSpec("x.n", (ParamSpec("n", "number"),), _fn)
    check_args(spec, [1])
    check_args(spec, [1.5])
    with pytest.raises(VerbError, match="n"):
        check_args(spec, [False])


def test_build_table_refuses_a_duplicate_verb_name() -> None:
    a = types.SimpleNamespace(VERBS=(_GET,))
    b = types.SimpleNamespace(VERBS=(_GET,))
    with pytest.raises(ValueError, match="memory.get"):
        verbs.build_table([a, b])


def test_build_table_refuses_a_name_outside_its_namespace_form() -> None:
    bad = VerbSpec("get", (), _fn)
    with pytest.raises(ValueError, match="get"):
        verbs.build_table([types.SimpleNamespace(VERBS=(bad,))])


def test_build_table_keys_by_name() -> None:
    table = verbs.build_table([types.SimpleNamespace(VERBS=(_GET, _SEARCH))])
    assert list(table) == ["memory.get", "memory.search"]
    assert table["memory.search"] is _SEARCH


def test_verb_table_discovers_only_verbs_modules() -> None:
    # Every discovered module is a verbs_* module of the package; with none
    # landed yet the table is empty, and each later step adds its own module.
    for spec in verbs.VERB_TABLE.values():
        assert spec.fn.__module__.startswith("nexus.lua_runtime.verbs_"), spec


def test_result_cap_is_set() -> None:
    assert 1 << 20 < verb_types.VERB_RESULT_CAP_BYTES < 64 << 20


def test_errors_carry_their_fields() -> None:
    e = errors.ScriptLimitError("wall_time", 60)
    assert (e.limit, e.value) == ("wall_time", 60)
    assert "wall_time" in str(e)
    s = errors.ScriptError("static_check", "global: x (line 3)")
    assert (s.kind, s.detail) == ("static_check", "global: x (line 3)")
    w = errors.ScriptWorkerError("eof", returncode=None, signal=11, stderr_head="boom")
    assert (w.returncode, w.signal, w.stderr_head) == (None, 11, "boom")
    assert issubclass(errors.ScriptBusyError, errors.ScriptRunError)
    for cls in (errors.ScriptError, errors.ScriptLimitError, errors.ScriptWorkerError):
        assert issubclass(cls, errors.ScriptRunError)


def test_verb_types_and_errors_import_in_a_bare_child() -> None:
    # The worker runs under python -I with an empty environment; the modules it
    # shares with the parent must import there without nexus configuration.
    # nexus._vcrt is the package's own stdlib-only import (src/nexus/__init__.py).
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import nexus.lua_runtime.verb_types, nexus.lua_runtime.errors, sys; "
            "bad = sorted(m for m in sys.modules if m.startswith('nexus.') "
            "and m not in ('nexus._vcrt', 'nexus.lua_runtime', "
            "'nexus.lua_runtime.verb_types', 'nexus.lua_runtime.errors')); print(bad)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", proc.stdout
