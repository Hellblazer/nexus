# SPDX-License-Identifier: AGPL-3.0-or-later
"""``run_procedure`` (RDR-240 Technical Design 2; nexus-qo7ie.31).

The runner reads no file and checks nothing: it calls ``nexus.lua_runtime.run_script``
(looked up at call time; the nexus-mdbds.9 wiring adds it) and passes its result and its
errors through. A fake ``run_script`` stands in for the runtime here.
"""
from __future__ import annotations

import pytest

import nexus.lua_runtime as rt
from nexus.lua_runtime import runner
from nexus.lua_runtime.errors import (
    ScriptBusyError,
    ScriptError,
    ScriptLimitError,
    ScriptWorkerError,
)
from nexus.lua_runtime.header import RepoProbeError
from nexus.lua_runtime.library import LibraryError


def _install(monkeypatch: pytest.MonkeyPatch, fn: object) -> None:
    monkeypatch.setattr(rt, "run_script", fn, raising=False)


def test_the_result_and_the_arguments_pass_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, object]] = []
    result = {"a": [1, 2.5, None, "s"], "b": {"c": True}}

    def fake(name: object, args: object) -> object:
        seen.append((name, args))
        return result

    _install(monkeypatch, fake)
    args = {"rdr": 240, "weird": object()}  # the runner checks nothing, whatever the shape
    assert runner.run_procedure("research_add", args) is result
    assert seen == [("research_add", args)]
    assert seen[0][1] is args


@pytest.mark.parametrize(
    "exc",
    [
        ScriptError("static_check", "global: load (line 3)"),
        ScriptError("header", "line 1: missing"),
        ScriptError("args", "rdr: missing"),
        ScriptLimitError("wall_time", 60.0),
        ScriptWorkerError("worker ended", returncode=3),
        ScriptBusyError("4 workers"),
    ],
    ids=lambda e: type(e).__name__ + getattr(e, "kind", getattr(e, "limit", "")),
)
def test_a_runtime_error_passes_through_as_the_same_object(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    def fake(name: str, args: dict) -> object:
        raise exc

    _install(monkeypatch, fake)
    with pytest.raises(type(exc)) as err:
        runner.run_procedure("x", {})
    assert err.value is exc


def test_run_script_is_looked_up_when_the_call_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(rt, "run_script", raising=False)
    with pytest.raises(runner.RunnerUnavailableError) as err:
        runner.run_procedure("x", {})  # absent now
    assert "run_script" in str(err.value)
    _install(monkeypatch, lambda name, args: "late")  # installed after the import of runner
    assert runner.run_procedure("x", {}) == "late"


def test_the_runner_reads_no_file_and_names_no_checker() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(runner))
    imported = {
        alias.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for alias in n.names
    }
    assert not imported & {"find", "procedures", "parse", "prepare_args", "check", "static_check"}, imported
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not called & {"open", "read_bytes", "read_text", "find", "parse", "prepare_args", "check"}


# -- error text: the kind and the detail --------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind", "needle"),
    [
        (ScriptError("static_check", "verb-ungranted: memory.get (line 4)"), "static_check", "memory.get"),
        (ScriptError("args", "rdr: missing required argument"), "args", "rdr"),
        (ScriptLimitError("instructions", 1000000), "limit", "instructions"),
        (ScriptWorkerError("worker ended with exit 3", returncode=3), "worker", "exit 3"),
        (ScriptBusyError("4 workers run"), "busy", "4 workers"),
        (LibraryError("shadows_shipped", "the repo procedure x shadows a shipped one"), "name", "shadows_shipped"),
        (LibraryError("not_found", "no procedure x"), "name", "not_found"),
        (LibraryError("unreadable", "x: PermissionError"), "name", "unreadable"),
        (runner.RunnerUnavailableError("run_script is not installed"), "unavailable", "run_script"),
        (RepoProbeError("git rev-parse did not run"), "repo", "git rev-parse"),
    ],
)
def test_describe_error_gives_the_kind_then_the_detail(exc: Exception, kind: str, needle: str) -> None:
    text = runner.describe_error(exc)
    assert text.startswith(f"{kind}: ")
    assert needle in text


def test_a_library_error_reads_name_reason_detail() -> None:
    assert runner.describe_error(LibraryError("not_found", "no procedure x")) == "name: not_found: no procedure x"


def test_every_runtime_error_kind_has_a_front_door_text_of_its_own_family() -> None:
    from nexus.lua_runtime.errors import KINDS

    for kind in KINDS:
        assert runner.describe_error(ScriptError(kind, "d")) == f"{kind}: d"


def test_a_script_run_error_the_runner_has_no_branch_for_still_reads_as_text() -> None:
    from nexus.lua_runtime.errors import ScriptRunError

    class Odd(ScriptRunError):
        pass

    text = runner.describe_error(Odd("odd end"))
    assert text == "Odd: odd end"
    assert runner.describe_error(ScriptRunError("plain")) == "ScriptRunError: plain"


def test_describe_error_does_not_swallow_an_error_it_does_not_know() -> None:
    assert runner.describe_error(ValueError("boom")) is None


def test_the_known_error_tuple_covers_every_family() -> None:
    from nexus.lua_runtime.errors import ScriptRunError

    assert {ScriptRunError, LibraryError, runner.RunnerUnavailableError, RepoProbeError} == set(runner.PROCEDURE_ERRORS)
