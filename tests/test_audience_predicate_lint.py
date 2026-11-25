# SPDX-License-Identifier: AGPL-3.0-or-later
"""T9 (nexus-sby49): a hook that calls an audience predicate does it safely.

``conexus/hooks/scripts/_audience.py`` answers in the safe direction on error,
but a hook that imports it has two ways to defeat that: the import itself can
raise (a missing or broken helper crashes the script at import time, which
Claude Code treats as a non-blocking error, so the guard silently turns off),
and a predicate call that sits outside the hook's wrapper escapes the
fail-open/fail-closed handling the hook already has.

For every hook script under ``conexus/hooks/scripts/`` that imports ``_audience``:

1. the import sits in a ``try`` whose handler catches ``Exception`` or
   ``BaseException``;
2. the handler binds each imported name to a fallback that answers in the safe
   direction: ``True`` for ``maintainer`` and ``nexus_repo`` (tightening), ``False``
   for ``account_flag``, an empty set for ``remote_test_hosts`` (both loosening),
   ``None`` for ``note_marker_only``;
3. every call to a predicate sits in a covered function: one handed to
   ``_lib.run_hook`` or ``never_fail``, one whose own ``try`` holds the call
   (a standalone ``main()``), or one only ever called from covered functions.

The checker is exercised on fixture sources below, so a lint that stopped
looking would fail here, and on every script, with a non-vacuity count.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPTS = PROJECT_ROOT / "conexus" / "hooks" / "scripts"

#: imported name -> what its fallback must answer.
SAFE_ANSWER: dict[str, object] = {
    "maintainer": True,
    "nexus_repo": True,
    "account_flag": False,
    "remote_test_hosts": "empty",
    "note_marker_only": None,
    "note_hook_error": None,
}
WRAPPERS = {"run_hook", "never_fail"}


def _imports_audience(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return node.module == "_audience"
    if isinstance(node, ast.Import):
        return any(a.name == "_audience" for a in node.names)
    return False


def _fallback_answer(fn: ast.AST) -> object:
    """What a fallback ``def``/``lambda`` returns: a constant, ``"empty"`` for an empty set, else ``"?"``."""
    ret: ast.AST | None = None
    if isinstance(fn, ast.Lambda):
        ret = fn.body
    elif isinstance(fn, ast.FunctionDef):
        rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        ret = rets[0].value if len(rets) == 1 else None
        if not rets:
            return None
    if ret is None:
        return None
    if isinstance(ret, ast.Constant):
        return ret.value
    if isinstance(ret, ast.Call) and getattr(ret.func, "id", "") in ("frozenset", "set") and not ret.args:
        return "empty"
    return "?"


def violations(source: str) -> list[str]:
    tree = ast.parse(source)
    out: list[str] = []
    imports = [n for n in ast.walk(tree) if _imports_audience(n)]
    if not imports:
        return out

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    local_names: dict[str, str] = {}  # local name -> imported name
    for imp in imports:
        if isinstance(imp, ast.ImportFrom):
            for alias in imp.names:
                local_names[alias.asname or alias.name] = alias.name
        # `import _audience` use is `_audience.<name>(...)`: handled by attribute calls below.
        guarded = False
        node: ast.AST | None = parents.get(imp)
        child: ast.AST = imp
        while node is not None:
            if isinstance(node, ast.Try) and child in node.body and any(
                h.type is None or getattr(h.type, "id", "") in ("Exception", "BaseException") for h in node.handlers
            ):
                guarded = True
                handlers = node.handlers
                break
            child, node = node, parents.get(node)
        if not guarded:
            out.append(f"line {imp.lineno}: the _audience import is not inside a try that catches Exception")
            continue
        if isinstance(imp, ast.ImportFrom):
            bound: dict[str, object] = {}
            for h in handlers:
                for stmt in h.body:
                    if isinstance(stmt, (ast.FunctionDef,)):
                        bound[stmt.name] = _fallback_answer(stmt)
                    elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Lambda):
                        for t in stmt.targets:
                            if isinstance(t, ast.Name):
                                bound[t.id] = _fallback_answer(stmt.value)
            for alias in imp.names:
                local = alias.asname or alias.name
                want = SAFE_ANSWER.get(alias.name, "?missing-from-SAFE_ANSWER")
                if local not in bound:
                    out.append(f"line {imp.lineno}: no fallback binds {local!r} in the except handler")
                elif bound[local] != want:
                    out.append(
                        f"line {imp.lineno}: the fallback for {local!r} answers {bound[local]!r}, must answer {want!r}"
                    )
        else:
            out.append(f"line {imp.lineno}: `import _audience` has no per-name fallback; import the predicates by name")

    # Predicate calls: by local name (from-import) only.
    covered: dict[str, bool] = {}
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    wrapped_names = {
        a.id
        for call in ast.walk(tree) if isinstance(call, ast.Call)
        and getattr(call.func, "attr", getattr(call.func, "id", "")) in WRAPPERS
        for a in call.args if isinstance(a, ast.Name)
    }

    def enclosing_function(node: ast.AST) -> ast.FunctionDef | None:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.FunctionDef):
                return cur
            cur = parents.get(cur)
        return None

    def inside_own_try(fn: ast.FunctionDef, node: ast.AST) -> bool:
        cur = parents.get(node)
        while cur is not None and cur is not fn:
            if isinstance(cur, ast.Try):
                return True
            cur = parents.get(cur)
        return False

    def calls_of(names: set[str]) -> list[ast.Call]:
        return [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names
        ]

    predicate_calls = calls_of(set(local_names))
    # Fixed point: a function is covered if wrapped, if every predicate call in it sits in its
    # own try, or if it is only ever called from covered functions.
    for name in funcs:
        covered[name] = name in wrapped_names
    changed = True
    while changed:
        changed = False
        for name, fn in funcs.items():
            if covered[name]:
                continue
            own = [c for c in predicate_calls if enclosing_function(c) is fn]
            if own and all(inside_own_try(fn, c) for c in own):
                covered[name] = True
                changed = True
                continue
            sites = [enclosing_function(c) for c in calls_of({name})]
            if sites and all(s is not None and covered.get(s.name, False) for s in sites):
                covered[name] = True
                changed = True
    for call in predicate_calls:
        fn = enclosing_function(call)
        if fn is None:
            out.append(f"line {call.lineno}: {call.func.id}() is called at module level")  # type: ignore[attr-defined]
        elif not covered[fn.name] and not inside_own_try(fn, call):
            out.append(f"line {call.lineno}: {call.func.id}() is called in {fn.name}(), which no wrapper covers")  # type: ignore[attr-defined]
    return out


# ---------------------------------------------------------------------------
# The checker itself, on fixture sources
# ---------------------------------------------------------------------------

GOOD = '''
import _lib
try:
    from _audience import maintainer as _maintainer, account_flag as _flag
except BaseException:
    def _maintainer(payload):
        return True
    _flag = lambda: False

def body(payload):
    if _maintainer(payload):
        pass
    helper()

def helper():
    return _flag()

_lib.run_hook(body)
'''

STANDALONE = '''
try:
    from _audience import maintainer as _m
except Exception:
    def _m(payload):
        return True

def main():
    try:
        return _m({})
    except Exception:
        return 0
'''


def test_the_checker_passes_the_two_accepted_shapes():
    assert violations(GOOD) == []
    assert violations(STANDALONE) == []


@pytest.mark.parametrize(
    ("label", "source", "needle"),
    [
        (
            "unguarded import",
            "from _audience import maintainer\n\ndef body(p):\n    return maintainer(p)\n\n_lib.run_hook(body)\n",
            "not inside a try",
        ),
        (
            "tightening fallback answers False",
            "try:\n    from _audience import maintainer\nexcept BaseException:\n"
            "    def maintainer(p):\n        return False\n\ndef body(p):\n    maintainer(p)\n\n_lib.run_hook(body)\n",
            "must answer True",
        ),
        (
            "loosening fallback answers True",
            "try:\n    from _audience import account_flag\nexcept BaseException:\n"
            "    def account_flag():\n        return True\n\ndef body(p):\n    account_flag()\n\n_lib.run_hook(body)\n",
            "must answer False",
        ),
        (
            "no fallback bound",
            "try:\n    from _audience import maintainer\nexcept BaseException:\n    pass\n\n"
            "def body(p):\n    maintainer(p)\n\n_lib.run_hook(body)\n",
            "no fallback binds",
        ),
        (
            "call outside any wrapper",
            "try:\n    from _audience import maintainer\nexcept BaseException:\n"
            "    def maintainer(p):\n        return True\n\ndef stray(p):\n    return maintainer(p)\n\n"
            "def body(p):\n    pass\n\n_lib.run_hook(body)\n",
            "no wrapper covers",
        ),
        (
            "call at module level",
            "try:\n    from _audience import maintainer\nexcept BaseException:\n"
            "    def maintainer(p):\n        return True\n\nX = maintainer({})\n",
            "module level",
        ),
        (
            "handler catches too little",
            "try:\n    from _audience import maintainer\nexcept KeyError:\n"
            "    def maintainer(p):\n        return True\n\ndef body(p):\n    maintainer(p)\n\n_lib.run_hook(body)\n",
            "not inside a try",
        ),
    ],
)
def test_the_checker_fails_each_unsafe_shape(label, source, needle):
    found = violations(source)
    assert any(needle in v for v in found), (label, found)


# ---------------------------------------------------------------------------
# Every real script
# ---------------------------------------------------------------------------


def _scripts() -> list[pathlib.Path]:
    return sorted(p for p in SCRIPTS.rglob("*.py") if p.name != "_audience.py" and "__pycache__" not in p.parts)


def test_every_script_that_imports_the_audience_helper_does_it_safely():
    examined = 0
    for path in _scripts():
        source = path.read_text(encoding="utf-8")
        if "_audience" not in source:
            continue
        examined += 1
        bad = violations(source)
        assert bad == [], f"{path.relative_to(PROJECT_ROOT)}: {bad}"
    assert examined >= 1, "no script imports _audience: the lint examined nothing"


def test_the_git_guard_is_among_the_scripts_examined():
    guard = SCRIPTS / "routing" / "subagent_git_write_requires_orchestrator.py"
    assert "from _audience import" in guard.read_text(encoding="utf-8")
    assert guard in _scripts()
