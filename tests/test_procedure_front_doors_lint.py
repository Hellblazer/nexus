# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two front doors of the procedure library call ``run_procedure`` and nothing else (RDR-240 Technical Design 3).

``procedure_run`` (the MCP tool, ``nexus/mcp/core.py``) and ``nx run`` (``nexus/commands/run_cmd.py``)
run a procedure through ``nexus.lua_runtime.runner.run_procedure`` only. A front door that did the
runtime's work itself (read a procedure file, called ``run_script``, parsed a header, checked
arguments) would be a second run path with its own rules, and the sandbox's guarantees
(RDR-242: the name rule, the static check, the grants, the limits) hold only for the one.

The check is on the body of the function that runs: every call in it must be on a short list of
rendering calls (JSON, echo, exit, the error text, the failure mark), it must call the runner
exactly once, and it must name none of the runtime's own pieces. The names ``_run_procedure`` and
``run_procedure`` must be the imports of the runner's function, not local definitions.
File-only, so it runs in the ``-m lint`` job; each rule has a planted violation below.
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "src" / "nexus" / "mcp" / "core.py"
RUN_CMD = ROOT / "src" / "nexus" / "commands" / "run_cmd.py"
RUNNER_MODULE = "nexus.lua_runtime.runner"

#: Names that belong to the runtime or the library; a front door body naming one is a second run path.
FORBIDDEN: frozenset[str] = frozenset({
    "run_script", "_run_script_with_stats", "find", "procedures", "shipped_root", "parse", "prepare_args",
    "check", "calibrate", "probe_repo", "repo_root", "repo_name", "open", "read_bytes", "read_text",
    "static_check", "header", "library", "_procedure_library", "supervisor", "worker", "runtime",
    "subprocess", "Popen", "run_bounded", "memory_get", "memory_put", "tables",
})

#: The calls each front door's run body may make besides the runner (rendering only).
CORE_ALLOWED: frozenset[str] = frozenset({
    "_run_procedure", "_describe_procedure_error", "_cap_text_result", "dumps", "_fail", "_mcp_tool_error",
})
CMD_ALLOWED: frozenset[str] = frozenset({"run_procedure", "describe_error", "echo", "exit", "dumps"})


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(hits) == 1, f"expected exactly one function {name}, found {len(hits)}"
    return hits[0]


def _body_nodes(fn: ast.FunctionDef) -> list[ast.AST]:
    """Every node of the function's BODY (not its decorators, annotations or defaults)."""
    return [n for stmt in fn.body for n in ast.walk(stmt)]


def _called(fn: ast.FunctionDef) -> list[str]:
    out: list[str] = []
    for n in _body_nodes(fn):
        if isinstance(n, ast.Call):
            out.append(n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "<dynamic>"))
    return out


def _names(fn: ast.FunctionDef) -> set[str]:
    out: set[str] = set()
    for n in _body_nodes(fn):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def front_door_findings(source: str, func: str, runner: str, allowed: frozenset[str]) -> list[str]:
    """What is wrong with the run body *func* in *source*; empty when it is clean."""
    tree = ast.parse(source)
    fn = _function(tree, func)
    findings: list[str] = []
    calls = _called(fn)
    if calls.count(runner) != 1:
        findings.append(f"{func} calls {runner} {calls.count(runner)} times, not once")
    extra = sorted(set(calls) - allowed)
    if extra:
        findings.append(f"{func} calls {extra}, which is not rendering")
    named = sorted(_names(fn) & FORBIDDEN)
    if named:
        findings.append(f"{func} names {named}, which belong to the runtime or the library")
    defs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == runner]
    if defs:
        findings.append(f"{runner} is defined in this module; it must be imported from {RUNNER_MODULE}")
    return findings


def runner_import_findings(source: str, alias: str) -> list[str]:
    """The module must bind *alias* by importing ``run_procedure`` from the runner module."""
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.ImportFrom) and n.module == RUNNER_MODULE:
            for a in n.names:
                if a.name == "run_procedure" and (a.asname or a.name) == alias:
                    return []
    return [f"{alias} is not `from {RUNNER_MODULE} import run_procedure`"]


def runner_use_findings(source: str, alias: str, owner: str) -> list[str]:
    """Outside *owner* the module never mentions the runner function."""
    tree = ast.parse(source)
    inside = {id(n) for n in _body_nodes(_function(tree, owner))}
    return [
        f"{alias} is used outside {owner} (line {n.lineno})"
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == alias and id(n) not in inside
    ]


# -- the real front doors ----------------------------------------------------


def test_procedure_run_calls_run_procedure_and_nothing_else() -> None:
    src = CORE.read_text(encoding="utf-8")
    assert front_door_findings(src, "procedure_run", "_run_procedure", CORE_ALLOWED) == []
    assert runner_import_findings(src, "_run_procedure") == []


def test_procedure_run_is_the_only_user_of_the_runner_in_the_tool_module() -> None:
    src = CORE.read_text(encoding="utf-8")
    assert runner_use_findings(src, "_run_procedure", "procedure_run") == []


def test_nx_run_calls_run_procedure_and_nothing_else() -> None:
    src = RUN_CMD.read_text(encoding="utf-8")
    assert front_door_findings(src, "_run", "run_procedure", CMD_ALLOWED) == []
    assert runner_import_findings(src, "run_procedure") == []
    assert runner_use_findings(src, "run_procedure", "_run") == []


def test_the_click_command_only_dispatches() -> None:
    fn = _function(ast.parse(RUN_CMD.read_text(encoding="utf-8")), "run_cmd")
    assert set(_called(fn)) <= {"_list", "_run", "_load_args", "UsageError"}, _called(fn)
    assert _called(fn).count("_run") == 1


# -- planted violations: each rule fires -------------------------------------

CLEAN = textwrap.dedent(
    """
    from nexus.lua_runtime.runner import run_procedure

    def _run(name, args):
        result = run_procedure(name, args)
        echo(dumps(result))
    """
)


def test_the_clean_shape_passes() -> None:
    assert front_door_findings(CLEAN, "_run", "run_procedure", CMD_ALLOWED) == []
    assert runner_import_findings(CLEAN, "run_procedure") == []


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("result = run_script(name, args)\n    echo(dumps(result))", "calls run_procedure 0 times"),
        ("run_procedure(name, args)\n    run_procedure(name, args)", "2 times"),
        ("run_procedure(name, args)\n    helper(name)", "'helper'"),
        ("run_procedure(name, args)\n    data = open(name).read()", "'open'"),
        ("run_procedure(name, args)\n    x = library.find(name, cwd=None)", "library"),
        ("run_procedure(name, args)\n    prepare_args(None, args)", "prepare_args"),
        ("run_procedure(name, args)\n    Path(name).read_text()", "read_text"),
    ],
)
def test_a_body_that_does_more_is_flagged(body: str, needle: str) -> None:
    src = "from nexus.lua_runtime.runner import run_procedure\n\ndef _run(name, args):\n    " + body + "\n"
    findings = front_door_findings(src, "_run", "run_procedure", CMD_ALLOWED)
    assert findings and any(needle in f for f in findings), findings


def test_a_local_definition_in_place_of_the_import_is_flagged() -> None:
    src = textwrap.dedent(
        """
        def run_procedure(name, args):
            return 1

        def _run(name, args):
            run_procedure(name, args)
        """
    )
    assert any("defined in this module" in f for f in front_door_findings(src, "_run", "run_procedure", CMD_ALLOWED))
    assert runner_import_findings(src, "run_procedure")


def test_an_import_of_something_else_under_the_alias_is_flagged() -> None:
    src = "from nexus.lua_runtime.library import find as run_procedure\n"
    assert runner_import_findings(src, "run_procedure")


def test_a_use_of_the_runner_outside_the_run_body_is_flagged() -> None:
    src = CLEAN + "\ndef sneaky(name):\n    return run_procedure(name, {})\n"
    assert runner_use_findings(src, "run_procedure", "_run")


def test_the_forbidden_list_names_every_piece_of_the_runtime_a_front_door_could_reach() -> None:
    from nexus.lua_runtime import header, library, static_check

    for fn in (library.find, library.procedures, header.parse, header.prepare_args, static_check.check):
        assert fn.__name__ in FORBIDDEN, fn.__name__
