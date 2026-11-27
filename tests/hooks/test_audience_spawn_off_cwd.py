# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every process ``_audience.py`` starts resolves its tool on PATH alone, never in the working directory (nexus-sby49.18, RDR-259).

Two legs, run on both byte copies. The AST leg: each ``subprocess`` spawn takes ``argv_off_cwd(_tool_argv(...))`` as its first
argument, ``_tool_argv`` (the builder the bare-argv lints cannot see through) is only ever an argument of ``argv_off_cwd``, and the
spawns that capture output under a timeout number three (the count the bounded-subprocess census pins by name). The behaviour
leg: with ``which_off_cwd`` replaced by a recorder, each spawn site asks it for its tool, so removing ``argv_off_cwd`` from a site
turns the test red.
"""
from __future__ import annotations

import ast
import shutil
import sys
import types
from pathlib import Path

import pytest

from tests.hooks.git_shim import init_repo

ROOT = Path(__file__).resolve().parents[2]
COPIES = [ROOT / "src" / "nexus" / "hooks" / "_audience.py", ROOT / "conexus" / "hooks" / "scripts" / "_audience.py"]
SPAWNERS = frozenset({"run", "Popen", "check_output", "check_call", "call"})


def _is_call_to(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def _spawns(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in SPAWNERS
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"
    ]


@pytest.mark.parametrize("path", COPIES, ids=["src", "plugin"])
def test_every_spawn_takes_argv_off_cwd_of_a_built_argv(path: Path) -> None:
    spawns = _spawns(path)
    assert len(spawns) == 3, f"{path.name} starts {len(spawns)} processes; the census names three"
    for call in spawns:
        first = call.args[0] if call.args else None
        ok = (
            first is not None and _is_call_to(first, "argv_off_cwd") and len(first.args) == 1  # type: ignore[attr-defined]
            and _is_call_to(first.args[0], "_tool_argv")  # type: ignore[attr-defined]
        )
        assert ok, f"{path.name}:{call.lineno}: a spawn's first argument must be argv_off_cwd(_tool_argv(...))"


@pytest.mark.parametrize("path", COPIES, ids=["src", "plugin"])
def test_the_argv_builder_is_only_ever_handed_to_the_resolver(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    uses = [n for n in ast.walk(tree) if _is_call_to(n, "_tool_argv")]
    assert len(uses) == 3
    for use in uses:
        parent = parents[id(use)]
        assert _is_call_to(parent, "argv_off_cwd") and parent.args[0] is use, f"{path.name}:{use.lineno}: _tool_argv outside argv_off_cwd"  # type: ignore[attr-defined]
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "subprocess"]


@pytest.mark.parametrize("path", COPIES, ids=["src", "plugin"])
def test_the_spawns_that_capture_output_under_a_timeout_number_three(path: Path) -> None:
    captured = [c for c in _spawns(path) if {"capture_output", "timeout"} <= {k.arg for k in c.keywords}]
    assert len(captured) == 3


def test_each_spawn_site_asks_the_resolver_for_its_tool(audience_copy, tmp_path, monkeypatch) -> None:
    asked: list[str] = []
    found = shutil.which("git")
    assert found, "the tool under test is required"

    def which_off_cwd(name: str) -> "str | None":
        asked.append(name)
        return found if name == "git" else None

    monkeypatch.setitem(sys.modules, "_exec_path", types.SimpleNamespace(which_off_cwd=which_off_cwd))
    repo = init_repo(tmp_path / "r", {".nexus/project.json": "{}"})
    gitdir = str(repo / ".git")
    audience_copy._run_cat_file(str(repo), gitdir)
    audience_copy._run_git(str(repo), gitdir, "rev-parse", "--verify", "HEAD")
    assert audience_copy._nx_version() is None  # the fake finds no nx, so the spawn raises FileNotFoundError, which the caller absorbs
    assert asked == ["git", "git", "nx"]
