# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The worker's hook for the static check (T2 nexus/rdr242-interfaces, section B; module owned by nexus-mdbds.6).

The worker calls ``static_check.check(source, grants, verbs)`` (which
calibrates on first use) and files a refusal as an error frame of kind
``static_check``, and a ``ChunkFormatError`` the same with ``calibrate: <msg>``. The hook tolerates the module's
absence, and only its absence. The end-to-end cases run the real worker in a
child whose ``static_check`` is the real module with ``check`` replaced, so a
case can force a refusal or a drift while the runtime still finds the
allowlists it imports.
"""

from __future__ import annotations

import sys
import textwrap
import types

import pytest

from nexus.lua_runtime import worker
from nexus.lua_runtime.errors import ScriptError
from nexus.lua_runtime.supervisor import WorkerConfig, supervise
from tests._module_seam import setattr_in
from tests.lua_runtime._support import limits, start, verb, verb_table

_NAME = "nexus.lua_runtime.static_check"


def _stub(calibrate_error: str | None = None, refuse: bool = False) -> types.ModuleType:
    """A stand-in with marlowe's interface: ``check`` calibrates on first use and may raise ChunkFormatError."""
    mod = types.ModuleType(_NAME)

    class ChunkFormatError(Exception):
        pass

    class StaticCheckRefusal(Exception):
        def __init__(self, rule: str, detail: str, line: int | None) -> None:
            super().__init__(
                f"{rule}: {detail}" + (f" (line {line})" if line is not None else "")
            )

    def check(
        source: bytes, grants: frozenset[str], verbs: frozenset[str]
    ) -> frozenset[str]:
        mod.calls.append(("check", source, grants, verbs))  # type: ignore[attr-defined]
        if calibrate_error:
            raise ChunkFormatError(calibrate_error)
        if refuse:
            raise StaticCheckRefusal("global", "print is not a granted name", 3)
        return frozenset()

    mod.calls = []  # type: ignore[attr-defined]
    mod.ChunkFormatError = ChunkFormatError  # type: ignore[attr-defined]
    mod.check = check  # type: ignore[attr-defined]
    mod.StaticCheckRefusal = StaticCheckRefusal  # type: ignore[attr-defined]
    return mod


def test_the_hook_passes_when_the_module_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules, _NAME, None
    )  # an import of it now raises ModuleNotFoundError
    assert worker._static_check(b"return 1", frozenset(), frozenset()) is None


def test_a_module_that_exists_but_fails_to_import_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(name: str) -> None:
        raise ModuleNotFoundError("No module named 'lupa'", name="lupa")

    setattr_in(monkeypatch, worker, "importlib.import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        worker._static_check(b"x", frozenset(), frozenset())


def test_check_gets_the_source_the_grants_and_every_verb_name_from_the_start_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _stub()
    monkeypatch.setitem(sys.modules, _NAME, mod)
    assert (
        worker._static_check(b"return 1", frozenset({"a.b"}), frozenset({"a.b", "c.d"}))
        is None
    )
    assert mod.calls == [
        ("check", b"return 1", frozenset({"a.b"}), frozenset({"a.b", "c.d"}))
    ]


def test_a_refusal_becomes_the_error_detail_with_rule_and_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, _NAME, _stub(refuse=True))
    assert (
        worker._static_check(b"print(1)", frozenset(), frozenset())
        == "global: print is not a granted name (line 3)"
    )


def test_a_chunk_format_error_from_the_first_use_calibration_is_reported_as_calibrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules, _NAME, _stub(calibrate_error="opcode table drifted")
    )
    assert (
        worker._static_check(b"x", frozenset(), frozenset())
        == "calibrate: opcode table drifted"
    )


_CHILD = textwrap.dedent(
    """
    import os, sys, types
    import nexus.lua_runtime.static_check as real
    # A stand-in that keeps the real module's constants (BASE_FUNCTIONS and
    # LIB_MEMBERS, which the runtime imports) and exception classes, and
    # replaces only check(), so each case can force a refusal or a drift.
    mod = types.ModuleType("nexus.lua_runtime.static_check")
    mod.__dict__.update({k: v for k, v in vars(real).items() if not k.startswith("__")})
    StaticCheckRefusal, ChunkFormatError = real.StaticCheckRefusal, real.ChunkFormatError
    def check(source, grants, verbs):
        if __BAD__:
            raise ChunkFormatError("layout drift")
        if b"print" in source:
            raise StaticCheckRefusal("global", "print", 1)
        pass
        return frozenset()
    mod.check = check
    sys.modules["nexus.lua_runtime.static_check"] = mod
    from nexus.lua_runtime import worker
    os._exit(worker.main(sys.argv[1:]))
    """
)


def _run(source: str, bad_calibrate: bool = False):
    argv = (
        sys.executable,
        "-I",
        "-c",
        _CHILD.replace("__BAD__", repr(bad_calibrate)),
        "--cpu-soft",
        "60",
        "--cpu-hard",
        "70",
        "--as-bytes",
        str(1 << 30),
    )
    table = verb_table(verb("fixture.echo", lambda s, deadline, ctx: s, "string"))
    return supervise(
        start(source, "fixture.echo"),
        limits=limits(30),
        verbs=table,
        config=WorkerConfig(argv=argv),
    )


def test_end_to_end_a_refusal_is_a_static_check_script_error_and_the_script_never_runs() -> (
    None
):
    with pytest.raises(ScriptError) as err:
        _run("print('x') return 1")
    assert (
        err.value.kind == "static_check"
        and err.value.detail == "global: print (line 1)"
    )


def test_end_to_end_a_calibration_failure_is_a_static_check_script_error() -> None:
    with pytest.raises(ScriptError) as err:
        _run("return 1", bad_calibrate=True)
    assert (
        err.value.kind == "static_check"
        and err.value.detail == "calibrate: layout drift"
    )


def test_end_to_end_a_script_the_check_passes_runs() -> None:
    assert _run("return 40 + 2").value == 42
