# SPDX-License-Identifier: AGPL-3.0-or-later
"""The T3 ``session`` argument is accepted and ignored (nexus-jlq15).

Since nexus-6pfgb.4 no chunk row carries a session id, so the MCP ``store_put``
``session`` argument and ``nx store put --session-id`` do nothing. Their text
must say so rather than promise an override. The behavioural half is an AST
read of the function body: a later change that makes ``session`` matter again
fails here and has to rewrite the text with it.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from nexus.commands.store import put_cmd
from nexus.mcp import core


def _store_put_fn():
    tool = core.mcp._tool_manager._tools["store_put"]
    return tool.fn


def test_mcp_store_put_session_description_says_ignored() -> None:
    props = core.mcp._tool_manager._tools["store_put"].parameters["properties"]
    desc = props["session"]["description"]
    assert "ignored" in desc.lower() and "deprecated" in desc.lower(), desc
    assert "override" not in desc.lower(), desc
    assert len(desc) < 120  # Python 3.12 counts indentation; keep it short


def test_cli_store_put_session_id_help_says_ignored() -> None:
    opt = next(p for p in put_cmd.params if p.name == "session_id")
    assert opt.help and "ignored" in opt.help.lower() and "deprecated" in opt.help.lower()


def test_store_put_body_never_reads_session() -> None:
    fn = _store_put_fn()
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    loads = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "session" and isinstance(n.ctx, ast.Load)
    ]
    assert loads == [], "store_put now reads `session`: the description must be rewritten"


def test_cli_put_cmd_body_never_reads_session_id() -> None:
    src = textwrap.dedent(inspect.getsource(put_cmd.callback))
    tree = ast.parse(src)
    loads = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "session_id" and isinstance(n.ctx, ast.Load)
    ]
    assert loads == []
