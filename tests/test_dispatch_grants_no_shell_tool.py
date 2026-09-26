# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 (nexus-wauo1.40): no dispatched ``claude -p`` child gets a shell tool.

The harness dispatch grant (``nexus.claude_child_env.apply_harness_oauth_grant``)
keeps ``NX_HARNESS_CLAUDE_OAUTH_TOKEN`` in a dispatched child's environment so
a nested nx-mcp can grant again. Claude deletes ``CLAUDE_CODE_OAUTH_TOKEN`` from
its own process.env, but not the harness name, so any shell the child spawns
would inherit the harness token. That is safe only while no dispatched child
can run a shell. These tests pin that invariant and fail when a call site
widens a grant.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus import aspect_extractor as ax
from nexus.mcp.core import _subprocess_tool_grant

SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"

# Built-in tools that execute a shell or drive one; a wildcard grants them all.
SHELL_TOOLS: frozenset[str] = frozenset({"Bash", "BashOutput", "KillShell", "KillBash", "*"})


def _grants_shell(tool: str) -> bool:
    base = tool.split("(", 1)[0].strip()
    return base in SHELL_TOOLS


def test_subprocess_tool_grant_has_no_shell_tool() -> None:
    _servers, allowed = _subprocess_tool_grant()
    assert allowed, "the grant is non-empty; an empty list would make this test vacuous"
    offending = [t for t in allowed if _grants_shell(t)]
    assert not offending, f"_subprocess_tool_grant grants a shell tool: {offending}"


def _call_name(node: ast.Call) -> str | None:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _grant_bound_names(func: ast.AST) -> set[str]:
    """Names assigned from ``_subprocess_tool_grant()`` inside ``func``."""
    names: set[str] = set()
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value) == "_subprocess_tool_grant"
        ):
            for target in node.targets:
                for elt in ast.walk(target):
                    if isinstance(elt, ast.Name):
                        names.add(elt.id)
    return names


def test_every_dispatch_allowed_tools_comes_from_the_audited_grant() -> None:
    """Every ``allowed_tools=`` passed to ``claude_dispatch`` in src/ is a name
    bound from ``_subprocess_tool_grant()`` in the same function, which the
    test above audits. A literal list, or any other source, fails here and
    has to be reviewed against the shell-tool invariant before it lands."""
    dispatch_calls = 0
    granted_calls = 0
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for func in funcs:
            bound = _grant_bound_names(func)
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and _call_name(node) == "claude_dispatch"):
                    continue
                dispatch_calls += 1
                for kw in node.keywords:
                    if kw.arg is None:
                        violations.append(f"{path.relative_to(SRC)}:{node.lineno} **kwargs unpacking hides the grant")
                    elif kw.arg == "allowed_tools":
                        granted_calls += 1
                        if not (isinstance(kw.value, ast.Name) and kw.value.id in bound):
                            violations.append(
                                f"{path.relative_to(SRC)}:{node.lineno} allowed_tools not from _subprocess_tool_grant()"
                            )
    # Non-vacuity: the scan must see the real call sites, including the two
    # tool-granting ones (nx_enrich_beads, nx_plan_audit).
    assert dispatch_calls >= 15, f"scan found only {dispatch_calls} claude_dispatch calls"
    assert granted_calls >= 2, f"scan found only {granted_calls} tool-granting dispatches"
    assert not violations, "\n".join(violations)


def test_tool_free_dispatch_disables_builtin_tools() -> None:
    """claude_dispatch with no allowed_tools passes ``--tools ""``, so the
    child has no Bash even under a permissive settings.json."""
    text = (SRC / "operators" / "dispatch.py").read_text(encoding="utf-8")
    assert 'argv += ["--tools", ""]' in text


def test_isolated_aspect_call_disables_builtin_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    class _Reaped:
        args = ["claude"]
        returncode = 0

        def communicate(self, *a, **k):
            return ('{"result": "{}"}', "")

    def _spy_popen(argv, **kw):
        captured.append(argv)
        return _Reaped()

    monkeypatch.setattr(ax.subprocess, "Popen", _spy_popen)
    ax._run_claude_isolated("x", timeout=1)
    argv = captured[0]
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == "", argv
    assert "--allowedTools" not in argv, argv
