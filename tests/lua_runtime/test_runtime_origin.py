# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 gate r3 S1 (nexus-mdbds.5): nothing under ``src/nexus/`` takes script source from a caller.

RDR-242 Technical Design 2: "no run path takes source". The run path is
``run_script(name, ...)``, which reads a library file; the source then travels
as the bytes of that file, from the parent to the worker's ``start`` frame to
``runtime.run``. A function elsewhere that took source (a ``run_source``, an
``eval_script``, a convenience for tests left in the library) would be a run
path the design forbids. "Test-only" is not a guard. This is.

The scan is over syntax, not behaviour: a ``def`` whose parameter is named for
a script's text, in any module under ``src/nexus/`` that defines or imports
anything of the Lua runtime. The functions below are the legitimate ones: the
worker's own chain, which only ever sees the bytes of a library file, and the
static check that reads them. Anything else fails this test, and adding a name
to the list is a decision a reviewer sees.

``supervisor.supervise`` takes the source too, but as the ``"source"`` key of
its start mapping, which a parameter scan cannot see. So a second scan pins
every call of ``supervise`` under ``src/nexus/``: ``run_script``'s own chain is
the only caller, and it passes the bytes ``library.find`` read.

The one source-taking entry point for tests is
``tests/lua_runtime/_runtime_support.run_lua``; it lives under ``tests/``.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

import nexus

SRC = pathlib.Path(nexus.__file__).parent

#: Parameter names that mean "the text or bytes of a script".
SOURCE_PARAMS = frozenset(
    {"source", "src", "script", "script_source", "lua_source", "lua_src", "lua_code", "chunk"}
)

#: ``module:qualified name`` of every function under ``src/nexus/`` that may take script source.
#: The worker chain (frames, static check, runtime) and the header reader of RDR-240; none is
#: reachable from a tool argument, a CLI argument or a caller of ``run_script``.
ALLOWED: frozenset[str] = frozenset(
    {
        # nexus-qo7ie.31: core.py imports the runner for procedure_run, so the scan now examines it.
        # These three MCP tools' public `source` parameter names a DATA source (items or aspects),
        # never script source; renaming it would break their callers.
        "nexus.mcp.core:operator_aggregate",
        "nexus.mcp.core:operator_filter",
        "nexus.mcp.core:operator_groupby",
        "nexus.lua_runtime.runtime:run",
        "nexus.lua_runtime.worker:_static_check",
        "nexus.lua_runtime.static_check:check",
        "nexus.lua_runtime.static_check:_check",
        "nexus.lua_runtime.static_check:_dump",
        "nexus.lua_runtime.static_check:_line_at",
        # RDR-240 Technical Design 1 (nexus-mdbds.9): reads the header lines of a library file's
        # bytes, in the parent; _split_lines is parse's own line splitter, called only by it
        "nexus.lua_runtime.header:parse",
        "nexus.lua_runtime.header:_split_lines",
    }
)


def _touches_lua_runtime(tree: ast.AST, module: str) -> bool:
    if module.startswith("nexus.lua_runtime"):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("nexus.lua_runtime"):
            return True
        if isinstance(node, ast.Import) and any(a.name.startswith("nexus.lua_runtime") for a in node.names):
            return True
    return False


def source_takers(root: pathlib.Path, package: str = "nexus") -> tuple[set[str], int]:
    """Every ``module:qualname`` under *root* with a script-source parameter; and modules examined."""
    found: set[str] = set()
    examined = 0
    for path in sorted(root.rglob("*.py")):
        module = ".".join((package, *path.relative_to(root).with_suffix("").parts))
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _touches_lua_runtime(tree, module):
            continue
        examined += 1

        def visit(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = child.args
                    names = {
                        a.arg
                        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                        + tuple(x for x in (args.vararg, args.kwarg) if x is not None)
                    }
                    if names & SOURCE_PARAMS:
                        found.add(f"{module}:{prefix}{child.name}")
                    visit(child, f"{prefix}{child.name}.")
                elif isinstance(child, ast.ClassDef):
                    visit(child, f"{prefix}{child.name}.")
                else:
                    visit(child, prefix)

        visit(tree, "")
    return found, examined


#: ``module:qualified name`` of every function under ``src/nexus/`` that calls the supervisor's
#: entry points. ``supervise`` takes script source in its start mapping (the ``"source"`` key), so a
#: new caller is a new run path: it must read the source from the library, as run_script does.
SUPERVISE_CALLERS: frozenset[str] = frozenset(
    {
        "nexus.lua_runtime.run:_run_script_with_stats",  # passes the bytes library.find read
        "nexus.lua_runtime.supervisor:supervise",  # the slot-holding wrapper of _supervise
    }
)
SUPERVISE_NAMES = frozenset({"supervise", "_supervise"})


def supervise_callers(root: pathlib.Path, package: str = "nexus") -> set[str]:
    """Every ``module:qualname`` under *root* whose body calls ``supervise`` or ``_supervise``."""
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        module = ".".join((package, *path.relative_to(root).with_suffix("").parts))
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _touches_lua_runtime(tree, module):
            continue

        def visit(node: ast.AST, prefix: str, owner: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, f"{prefix}{child.name}.", f"{prefix}{child.name}")
                elif isinstance(child, ast.ClassDef):
                    visit(child, f"{prefix}{child.name}.", owner)
                else:
                    if isinstance(child, ast.Call):
                        fn = child.func
                        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                        if name in SUPERVISE_NAMES:
                            found.add(f"{module}:{owner or '<module>'}")
                    visit(child, prefix, owner)

        visit(tree, "", "")
    return found


def test_only_run_scripts_chain_calls_the_supervisor() -> None:
    found = supervise_callers(SRC)
    assert "nexus.lua_runtime.run:_run_script_with_stats" in found, (
        "the scan does not see run_script's own call of supervise: it is vacuous"
    )
    assert found <= SUPERVISE_CALLERS, f"new callers of supervise under src/nexus: {sorted(found - SUPERVISE_CALLERS)}"


def test_the_supervise_scan_sees_a_planted_wrapper(tmp_path: pathlib.Path) -> None:
    """A convenience wrapper that hands caller text to supervise goes red."""
    (tmp_path / "wrapper.py").write_text(
        textwrap.dedent(
            """
            from nexus.lua_runtime import supervisor

            def eval_text(text: str) -> object:
                return supervisor.supervise({"name": "x", "source": text})
            """
        )
    )
    (tmp_path / "unrelated.py").write_text("def supervise(x): return x\n\ndef f(): return supervise(1)\n")
    assert supervise_callers(tmp_path) == {"nexus.wrapper:eval_text"}


def test_no_function_under_src_takes_script_source_except_the_workers_chain() -> None:
    found, examined = source_takers(SRC)
    assert examined >= 8, f"the scan examined {examined} Lua-runtime modules; it is not looking"
    assert "nexus.lua_runtime.runtime:run" in found, "the scan does not see runtime.run: it is vacuous"
    assert found <= ALLOWED, f"new script-source takers under src/nexus: {sorted(found - ALLOWED)}"


def test_the_public_names_of_the_package_take_no_source() -> None:
    """The package's exports are the run path a caller sees (run_script, the errors)."""
    init = (SRC / "lua_runtime" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(init)
    exported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            exported.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            exported.update(ast.literal_eval(node.value))
    assert not exported & {"run", "supervise", "check", "parse"}, sorted(exported)


def test_the_scan_sees_a_planted_function_and_a_planted_import(tmp_path: pathlib.Path) -> None:
    """The guard goes red on the thing it guards against."""
    pkg = tmp_path / "lua_runtime"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "convenient.py").write_text(
        textwrap.dedent(
            """
            def run_source(source: str) -> object:
                return source
            class Runner:
                def go(self, lua_code: bytes) -> None: ...
            """
        )
    )
    (tmp_path / "elsewhere.py").write_text(
        "from nexus.lua_runtime import runtime\n\ndef quick(script: str) -> None: ...\n"
    )
    (tmp_path / "unrelated.py").write_text("def read(source: str) -> str: return source\n")
    found, examined = source_takers(tmp_path)
    assert found == {
        "nexus.lua_runtime.convenient:run_source",
        "nexus.lua_runtime.convenient:Runner.go",
        "nexus.elsewhere:quick",
    }
    assert examined == 3  # the unrelated module is not a Lua-runtime module
