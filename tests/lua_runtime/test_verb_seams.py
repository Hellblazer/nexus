# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 shared seams (T2 nexus/rdr242-interfaces, section A).

The verb types, the run errors and the verb table that the worker
(nexus-mdbds.4), the memory and tables verbs (nexus-mdbds.7) and the git and
transcripts verbs (nexus-mdbds.8) all build on.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from nexus.lua_runtime import errors, verb_types, verbs
from tests._module_seam import setattr_in
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


def test_check_args_returns_the_normalized_arguments() -> None:
    assert check_args(_GET, ["p", "t"]) == ("p", "t")
    assert check_args(_SEARCH, ["q", "p", 5]) == ("q", "p", 5)
    assert check_args(_SEARCH, ["q", "p", None]) == ("q", "p", None)
    # a trailing optional may be omitted; it is handed over as None
    assert check_args(_SEARCH, ["q", "p"]) == ("q", "p", None)


def test_integer_takes_a_lua_float_with_no_fraction_as_an_int() -> None:
    # In Lua 5.4, 10/2 is the float 5.0 and reaches the parent as 5.0.
    out = check_args(_SEARCH, ["q", "p", 5.0])
    assert out == ("q", "p", 5)
    assert type(out[2]) is int


@pytest.mark.parametrize(
    "value", [float("inf"), float("-inf"), float("nan"), 2.0**70, 2**63, -(2**63) - 1]
)
def test_integer_refuses_what_no_lua_integer_can_be(value: object) -> None:
    with pytest.raises(VerbError, match="limit"):
        check_args(_SEARCH, ["q", "p", value])


def test_number_refuses_non_finite() -> None:
    spec = VerbSpec("x.n", (ParamSpec("n", "number"),), _fn)
    with pytest.raises(VerbError, match="n must be finite"):
        check_args(spec, [float("nan")])


def test_boolean_and_string_refuse_each_other_and_numbers() -> None:
    spec = VerbSpec("x.b", (ParamSpec("b", "boolean"), ParamSpec("s", "string")), _fn)
    assert check_args(spec, [True, "x"]) == (True, "x")
    with pytest.raises(VerbError, match="b must be a boolean"):
        check_args(spec, [1, "x"])
    with pytest.raises(VerbError, match="s must be a string"):
        check_args(spec, [True, True])


@pytest.mark.parametrize("args", ["ab", {"a": 1, "b": 2}, ("p", "t")])
def test_check_args_refuses_arguments_that_are_not_a_list(args: object) -> None:
    with pytest.raises(VerbError, match="must be a list"):
        check_args(_GET, args)


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
    with pytest.raises(VerbError, match="n must be a number"):
        check_args(spec, [False])


def test_build_table_refuses_a_duplicate_verb_name() -> None:
    a = types.SimpleNamespace(VERBS=(_GET,))
    b = types.SimpleNamespace(VERBS=(_GET,))
    with pytest.raises(ValueError, match="memory.get"):
        verbs.build_table([a, b])


@pytest.mark.parametrize(
    "name",
    ["get", "memory.get\n", "Memory.get", "a.b.c", "string.x", "end.get", "memory.end"],
)
def test_a_verb_name_outside_its_form_is_refused_at_construction(name: str) -> None:
    with pytest.raises(ValueError, match="verb name"):
        VerbSpec(name, (), _fn)


def test_a_required_parameter_after_an_optional_one_is_refused() -> None:
    with pytest.raises(ValueError, match="follows an optional"):
        VerbSpec("x.y", (ParamSpec("a", "string", True), ParamSpec("b", "string")), _fn)


def test_duplicate_parameter_names_and_a_non_callable_fn_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate parameter"):
        VerbSpec("x.y", (ParamSpec("a", "string"), ParamSpec("a", "string")), _fn)
    with pytest.raises(ValueError, match="not callable"):
        VerbSpec("x.y", (), "nope")  # type: ignore[arg-type]


def test_build_table_refuses_a_module_without_verbs_or_a_non_spec() -> None:
    with pytest.raises(ValueError, match="exports no VERBS"):
        verbs.build_table([types.SimpleNamespace()])
    with pytest.raises(ValueError, match="is not a VerbSpec"):
        verbs.build_table([types.SimpleNamespace(VERBS=("memory.get",))])


def test_build_table_keys_by_name() -> None:
    table = verbs.build_table([types.SimpleNamespace(VERBS=(_GET, _SEARCH))])
    assert list(table) == ["memory.get", "memory.search"]
    assert table["memory.search"] is _SEARCH


_SPEC_SRC = (
    "from nexus.lua_runtime.verb_types import VerbSpec\n"
    "def fn(*a, deadline): return None\n"
    "VERBS = (VerbSpec({name!r}, (), fn),)\n"
)


def _fake_package(tmp_path: Path, files: dict[str, str]) -> types.ModuleType:
    pkg_name = f"fakeverbs_{tmp_path.name}"
    root = tmp_path / pkg_name
    root.mkdir()
    (root / "__init__.py").write_text("")
    for name, body in files.items():
        (root / f"{name}.py").write_text(body)
    sys.path.insert(0, str(tmp_path))
    try:
        return importlib.import_module(pkg_name)
    finally:
        sys.path.remove(str(tmp_path))


def test_discovery_imports_only_verbs_modules_in_sorted_order(tmp_path: Path) -> None:
    pkg = _fake_package(
        tmp_path,
        {
            "verbs_b": _SPEC_SRC.format(name="bee.get"),
            "verbs_a": _SPEC_SRC.format(name="ay.get"),
            "helper": "raise RuntimeError('helper must not be imported')\n",
        },
    )
    mods = verbs.verb_modules(pkg)
    assert [m.__name__.rsplit(".", 1)[1] for m in mods] == ["verbs_a", "verbs_b"]
    assert f"{pkg.__name__}.helper" not in sys.modules
    assert list(verbs.build_table(mods)) == ["ay.get", "bee.get"]


def test_discovery_sorts_whatever_order_the_listing_gives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _fake_package(
        tmp_path,
        {
            "verbs_a": _SPEC_SRC.format(name="ay.get"),
            "verbs_b": _SPEC_SRC.format(name="bee.get"),
        },
    )
    real = pkgutil.iter_modules
    setattr_in(
        monkeypatch,
        verbs,
        "pkgutil.iter_modules",
        lambda path: list(reversed(list(real(path)))),
    )
    assert [m.__name__.rsplit(".", 1)[1] for m in verbs.verb_modules(pkg)] == [
        "verbs_a",
        "verbs_b",
    ]


def test_discovery_refuses_a_duplicate_across_modules(tmp_path: Path) -> None:
    pkg = _fake_package(
        tmp_path,
        {
            "verbs_a": _SPEC_SRC.format(name="memory.get"),
            "verbs_b": _SPEC_SRC.format(name="memory.get"),
        },
    )
    with pytest.raises(ValueError, match="defined twice"):
        verbs.build_table(verbs.verb_modules(pkg))


def test_discovery_names_a_verb_module_that_fails_to_import(tmp_path: Path) -> None:
    pkg = _fake_package(tmp_path, {"verbs_bad": "import no_such_module_xyz\n"})
    with pytest.raises(RuntimeError, match="verbs_bad failed to import"):
        verbs.verb_modules(pkg)


def test_verb_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        verbs.VERB_TABLE["x.y"] = _GET  # type: ignore[index]


def test_each_shipped_verb_module_imports_without_the_table() -> None:
    # The cycle rule: a verbs_* module imports verb_types, never verbs.
    for mod in verbs.verb_modules():
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys, {mod.__name__}; "
                "print('nexus.lua_runtime.verbs' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False", mod.__name__


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


def _bare_env() -> dict[str, str]:
    # RDR-242 item 6: the worker's environment is empty, with only SYSTEMROOT
    # on native Windows (Python does not start there without it).
    if sys.platform == "win32":
        return {"SYSTEMROOT": os.environ["SYSTEMROOT"]}
    return {}


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
        env=_bare_env(),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", proc.stdout
