# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo-wide class guard (nexus-cnzei.2 fix round 2, critic Significant):
any ``conexus/hooks/scripts/**/*.py`` file with a REAL ``import nexus`` /
``from nexus import ...`` statement (AST-detected, not a substring grep --
a comment or docstring mentioning "nexus" must never count) must call
``_hook_logging.configure_hook_logging()`` before its first such import,
in ACTUAL EXECUTION order, not merely earlier in the file's text.

Origin: the S2 fix (rdr_hook.py, routing/phase_review_close_requires_gate.py)
was applied at the INSTANCE level with no shared enforcement -- a third hook
script importing ``nexus.*`` in the future would reintroduce the exact
stdout-leak defect nexus-cnzei.2 item 2 fixed, with nothing here to catch
it. This test is that catch.

WHY EXECUTION ORDER, NOT TEXT ORDER. The two known files have different
shapes: ``rdr_hook.py``'s ``main()`` calls the guard, THEN calls a separate
helper function (``_resolve_rdr_collection``, defined EARLIER in the file
by line number) whose body contains the nexus import; a naive
"first-guard-call-line < first-import-line" comparison across the whole
file is wrong here (373 vs 93) even though the guard genuinely runs first
at runtime. ``phase_review_close_requires_gate.py``'s ``_claude_pid()``
calls the guard and does the import in the SAME function, back to back.
This module resolves that by walking the actual (bounded) call graph from
the file's real entry point(s) -- ``if __name__ == "__main__": main()``,
and the routing-hook framework's documented contract that
``_lib.run_hook(body, ...)`` invokes its first positional argument as the
hook body callback -- rather than comparing raw line numbers.

Watch-it-fail discipline (per this repo's testing standard): this test was
verified to go RED when the ``_hook_logging.configure_hook_logging()`` call
was temporarily removed from ``rdr_hook.py`` during authoring, then restored
green -- see the bead's own report for the transcript of that round-trip.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

HOOKS_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
)

#: The guard's OWN implementation module. It has a deferred
#: ``from nexus.logging_setup import configure_logging`` import inside
#: :func:`configure_hook_logging` itself -- requiring it to call itself
#: before its own import would be circular nonsense, not a real instance
#: of the S2 defect class (this module IS the guard; nothing calls it
#: before its own single, self-contained import).
_EXEMPT_RELATIVE_PATHS = frozenset({"_hook_logging.py"})

#: Known files this guard MUST find and check, so a bug that makes the
#: file-discovery walk or the AST detection return nothing (e.g. a wrong
#: glob, or a rewrite that changes both files' import shape at once)
#: reads as a pass, not a silent vacuity (nexus-moht0 doctrine).
_KNOWN_NEXUS_IMPORTING_FILES = frozenset({
    "rdr_hook.py",
    "routing/phase_review_close_requires_gate.py",
})


def _all_hook_script_files() -> list[Path]:
    return sorted(HOOKS_SCRIPTS_DIR.rglob("*.py"))


def _relative_path(path: Path) -> str:
    return str(path.relative_to(HOOKS_SCRIPTS_DIR))


def _module_name_from_import(node: ast.stmt) -> str | None:
    """The dotted module name a real ``import``/``from`` statement
    references, or ``None`` for anything else."""
    if isinstance(node, ast.ImportFrom):
        return node.module
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else None
    return None


def _is_nexus_import(node: ast.stmt) -> bool:
    name = _module_name_from_import(node)
    return name is not None and (name == "nexus" or name.startswith("nexus."))


def _nexus_import_lines(tree: ast.Module) -> list[int]:
    """Line numbers of every real (AST-detected) ``nexus``/``nexus.*``
    import ANYWHERE in the file (module scope or nested inside a
    function) -- used only to decide whether a file is in scope for this
    guard at all; the actual pass/fail check below is execution-order
    aware, not this raw list."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _is_nexus_import(node)
    )


def _direct_events(
    scope_node: ast.AST, functions_by_name: dict[str, ast.FunctionDef]
) -> list[tuple[int, str, str | None]]:
    """Events directly in *scope_node*'s own body (a function, or the
    whole module treated as one scope), in body order, NOT descending
    into nested function/class definitions (those are separate call
    targets with their own frame, visited only when actually called):

    ``("import", lineno)`` -- a real nexus.* import statement.
    ``("guard", lineno)`` -- a call that resolves to
      ``configure_hook_logging`` (``_hook_logging.configure_hook_logging()``
      or a bare ``configure_hook_logging()``).
    ``("call", lineno, target)`` -- a direct call to another
      locally-defined function, OR the routing-hook framework's
      documented ``run_hook(body, ...)`` / ``_lib.run_hook(body, ...)``
      contract (its first positional argument is invoked as the hook
      body callback -- an IMPLICIT call this walker must also see).
    """
    events: list[tuple[int, str, str | None]] = []
    # ``end_lineno`` is typed ``int | None`` (unset only on a synthetically
    # built node, never on one ``ast.parse`` produced from real source --
    # but this guard fails CLOSED rather than trusting that): a node with
    # no known end falls back to a single-line range at its own start,
    # which UNDER-excludes rather than over-excludes -- the fail direction
    # that makes a real import/guard-call MISS visible as a loud assertion
    # failure below, never as a silently-passed check.
    nested_ranges: list[tuple[int, int]] = [
        (n.lineno, n.end_lineno if n.end_lineno is not None else n.lineno)
        for n in ast.walk(scope_node)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n is not scope_node
    ]

    def _in_nested(lineno: int) -> bool:
        return any(start <= lineno <= end for start, end in nested_ranges)

    for node in ast.walk(scope_node):
        if node is scope_node:
            continue
        lineno = getattr(node, "lineno", None)
        if lineno is None or _in_nested(lineno):
            continue

        if isinstance(node, (ast.Import, ast.ImportFrom)) and _is_nexus_import(node):
            events.append((lineno, "import", None))
            continue

        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "configure_hook_logging") or (
            isinstance(func, ast.Name) and func.id == "configure_hook_logging"
        ):
            events.append((lineno, "guard", None))
            continue

        if isinstance(func, ast.Name) and func.id in functions_by_name:
            events.append((lineno, "call", func.id))

        is_run_hook = (isinstance(func, ast.Attribute) and func.attr == "run_hook") or (
            isinstance(func, ast.Name) and func.id == "run_hook"
        )
        if is_run_hook and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Name) and first_arg.id in functions_by_name:
                events.append((lineno, "call", first_arg.id))

    events.sort(key=lambda e: e[0])
    return events


def _first_reachable_event(
    scope_node: ast.AST,
    functions_by_name: dict[str, ast.FunctionDef],
    visited: frozenset[str],
) -> str | None:
    """DFS from *scope_node* (a function or the module) following DIRECT
    calls to other locally-defined functions, in body order, returning
    the first ``"guard"`` or ``"import"`` event reached -- or ``None`` if
    this scope (and everything it calls, transitively) has neither."""
    key = getattr(scope_node, "name", "<module>")
    if key in visited:
        return None  # recursion guard; a cycle has nothing new to say
    visited = visited | {key}
    for _lineno, kind, extra in _direct_events(scope_node, functions_by_name):
        if kind in ("guard", "import"):
            return kind
        if kind == "call" and extra in functions_by_name:
            result = _first_reachable_event(functions_by_name[extra], functions_by_name, visited)
            if result is not None:
                return result
    return None


def _first_event_from_entry_points(tree: ast.Module) -> str | None:
    functions_by_name = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    return _first_reachable_event(tree, functions_by_name, frozenset())


_ALL_FILES = _all_hook_script_files()
assert _ALL_FILES, (
    f"no .py files found under {HOOKS_SCRIPTS_DIR} -- the discovery glob "
    f"itself is broken, which would make every check below vacuously pass"
)


@pytest.mark.parametrize(
    "path", _ALL_FILES, ids=[_relative_path(p) for p in _ALL_FILES],
)
def test_nexus_importing_hook_script_configures_logging_first(path: Path) -> None:
    rel = _relative_path(path)
    if rel in _EXEMPT_RELATIVE_PATHS:
        pytest.skip(f"{rel} is the guard's own implementation module")

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    import_lines = _nexus_import_lines(tree)
    if not import_lines:
        pytest.skip(f"{rel} has no real nexus.* import (AST-checked)")

    first_event = _first_event_from_entry_points(tree)
    assert first_event != "import", (
        f"{rel} imports nexus.* (first at line {min(import_lines)}), and this "
        f"guard's execution-order walk from the file's entry point(s) "
        f"(if __name__ == '__main__': main(), or run_hook(body, ...)) reaches "
        f"that import before any call to _hook_logging.configure_hook_logging() "
        f"-- structlog's default stdout logger factory will leak debug/warning "
        f"lines into this hook's own stdout (nexus-cnzei.2 S2). Call "
        f"_hook_logging.configure_hook_logging() before the nexus import."
    )
    assert first_event == "guard", (
        f"{rel} imports nexus.* (first at line {min(import_lines)}) but this "
        f"guard's execution-order walk could not find EITHER a guard call or "
        f"the import reachable from the file's known entry-point shapes -- "
        f"the walker likely does not understand this file's entry pattern; "
        f"extend _direct_events/_first_event_from_entry_points rather than "
        f"assuming this is safe."
    )


def test_the_guard_actually_checked_the_known_files() -> None:
    """Non-vacuity (nexus-moht0 doctrine): if the discovery walk, the AST
    detection, or the exemption set silently stopped finding
    rdr_hook.py/phase_review_close_requires_gate.py, every parametrized
    case above could read as "skipped: no nexus import" and the whole
    file would report all-green having checked nothing real."""
    checked = set()
    for path in _ALL_FILES:
        rel = _relative_path(path)
        if rel in _EXEMPT_RELATIVE_PATHS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _nexus_import_lines(tree):
            checked.add(rel)
    missing = _KNOWN_NEXUS_IMPORTING_FILES - checked
    assert not missing, (
        f"expected these known nexus-importing hook scripts to be found and "
        f"checked, but they were not (discovery or AST-detection regressed): "
        f"{sorted(missing)}"
    )
