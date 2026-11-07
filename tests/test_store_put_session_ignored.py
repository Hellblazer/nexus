# SPDX-License-Identifier: AGPL-3.0-or-later
"""The T3 ``session`` argument is accepted and ignored (nexus-jlq15).

Since nexus-6pfgb.4 no new chunk row carries a session id, so the MCP
``store_put`` ``session`` argument and ``nx store put --session-id`` do
nothing. Their text must say so rather than promise an override. The
behavioural half is an AST read of the function body, and it is a PROSE
TRIPWIRE, not a proof: it catches a direct read, an alias, a closure, an
f-string, an augmented assignment, a ``locals()``/``vars()`` call and a string
key equal to the parameter name (``ctx.params["session_id"]``). It cannot see
a read through ``sys._getframe`` or ``inspect``. A later change that makes
``session`` matter again is meant to fail here and rewrite the text with it.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from nexus.commands.store import put_cmd
from nexus.mcp import core


def _store_put_fn():
    tool = core.mcp._tool_manager._tools["store_put"]
    return tool.fn


def _reads_of(fn_source: str, name: str) -> list[str]:
    """Every way the function body can read parameter ``name`` that we can see."""
    tree = ast.parse(textwrap.dedent(fn_source))
    hits: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
            hits.append(f"load of {name} at line {n.lineno}")
        elif (
            isinstance(n, ast.AugAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == name
        ):
            hits.append(f"augmented assignment of {name} at line {n.lineno}")
        elif (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in {"locals", "vars"}
        ):
            hits.append(f"{n.func.id}() call at line {n.lineno}")
        elif isinstance(n, ast.Constant) and n.value == name:
            hits.append(f"string key {name!r} at line {n.lineno}")
    return hits


def test_mcp_store_put_session_description_says_ignored() -> None:
    props = core.mcp._tool_manager._tools["store_put"].parameters["properties"]
    desc = props["session"]["description"]
    assert "ignored" in desc.lower() and "deprecated" in desc.lower(), desc
    assert "stores no session id" in desc.lower(), desc
    assert "override" not in desc.lower(), desc
    assert len(desc) < 120  # Python 3.12 counts indentation; keep it short


def test_cli_store_put_session_id_help_says_ignored() -> None:
    opt = next(p for p in put_cmd.params if p.name == "session_id")
    assert opt.help and "ignored" in opt.help.lower() and "deprecated" in opt.help.lower()
    assert "stores no session id" in opt.help.lower(), opt.help


def test_store_put_body_never_reads_session() -> None:
    loads = _reads_of(inspect.getsource(_store_put_fn()), "session")
    assert loads == [], f"store_put now reads `session`: rewrite the description ({loads})"


def test_cli_put_cmd_body_never_reads_session_id() -> None:
    loads = _reads_of(inspect.getsource(put_cmd.callback), "session_id")
    assert loads == [], f"put_cmd now reads `session_id`: rewrite the help ({loads})"


@pytest.mark.parametrize(
    "src",
    [
        "def f(session):\n    return session\n",
        "def f(session):\n    return locals()\n",
        "def f(session):\n    return vars()['x']\n",
        "def f(session, ctx):\n    return ctx.params['session']\n",
        "def f(session):\n    session += 'x'\n",
        "def f(session):\n    return f'{session}'\n",
    ],
)
def test_the_tripwire_sees_each_kind_of_read(src: str) -> None:
    assert _reads_of(src, "session"), src


def test_the_tripwire_passes_a_body_that_ignores_the_parameter() -> None:
    assert _reads_of("def f(session):\n    return 1\n", "session") == []
