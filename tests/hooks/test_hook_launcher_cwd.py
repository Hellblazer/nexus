# SPDX-License-Identifier: AGPL-3.0-or-later
"""A uv-launched hook never runs an interpreter from a ``.venv`` it did not ask for.

RDR-224 review, finding C (nexus-f9bgu.36). Claude Code runs a hook with the
project as its cwd. ``uv run --no-project --no-config <script>`` still searches
the cwd and its parents for a virtual environment while it looks for an
interpreter, and it EXECUTES what it finds there to read the version: a
``.venv/bin/python`` committed to a cloned repository ran on every uv-launched
hook, on every platform (measured on macOS, uv 0.8.0; it ran under ``--python``,
``--python-preference`` and ``--managed-python`` too). ``python3`` as a launcher
never had this: it is the PATH's.

The first cure, ``uv run --directory ${CLAUDE_PLUGIN_ROOT}``, only moved where the
search starts. The plugin root is ``~/.claude/plugins/cache/<marketplace>/<plugin>/
<version>``, so the search still walks up through the user's home and the drive
root: a ``~/.venv``, or a ``C:\\.venv`` that any local user on Windows may create,
ran on every hook. Measured in a real Claude Code session on Windows 11 (uv
0.12.23): seven of eleven entries ran a ``.venv`` planted in the home directory.

``uv tool run --python >=3.12 python <script>`` discovers no virtual environment
at all: tool environments are built from a managed or PATH interpreter only.
``--directory ${CLAUDE_PLUGIN_ROOT}`` stays, so a script's process cwd is the
plugin root and it takes the project from the payload's ``cwd`` (or
``CLAUDE_PROJECT_DIR``), never from the process.

Each case runs the real argv from hooks.json, exec form, with the project as cwd,
a copy of the plugin installed under a fake home, and a marker-writing
interpreter planted in the project, in its parent, and in the home above the
plugin root. The two ``..._plant_is_live_...`` tests are the non-vacuity
controls: the same plants under the two earlier launcher forms, and the marker
appears.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._hook_wiring import REPO_ROOT, UV_LAUNCHER_ARGV

_PLUGINS = ("conexus", "sn")
_NAMES = ("python", "python3", "python3.12", "python3.13")

#: The launcher forms this file proves unsafe; each control rebuilds one from the
#: real entry's script and trailing args.
_RUN_FROM_CWD = ("run", "--no-project", "--no-config", "--quiet")
_RUN_FROM_PLUGIN_ROOT = ("run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "--no-project", "--no-config", "--quiet")


def _uv_entries() -> list[tuple[str, str, dict]]:
    out = []
    for plugin in _PLUGINS:
        data = json.loads((REPO_ROOT / plugin / "hooks" / "hooks.json").read_text())
        for event, groups in data["hooks"].items():
            for group in groups:
                for hook in group.get("hooks", []):
                    if hook.get("command") == "uv":
                        out.append((plugin, event, hook))
    return out


def _plant(directory: Path, marker: Path) -> None:
    bindir = directory / ".venv" / "bin"
    bindir.mkdir(parents=True)
    for name in _NAMES:
        exe = bindir / name
        exe.write_text(f'#!/bin/sh\necho "{name} from {directory}" >> "{marker}"\nexit 1\n')
        exe.chmod(0o755)
    (directory / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.12.0\n")


def _layout(plugin: str, tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(plugin_root, project, marker)``: the plugin installed under a planted home,
    and a planted project (and parent) outside it."""
    marker = tmp_path / "marker"
    home = tmp_path / "home"
    root = home / ".claude" / "plugins" / "cache" / "nexus-plugins" / plugin / "0.0.0"
    shutil.copytree(REPO_ROOT / plugin / "hooks", root / "hooks")
    _plant(home, marker)
    outer = tmp_path / "outer"
    project = outer / "project"
    project.mkdir(parents=True)
    _plant(project, marker)
    _plant(outer, marker)
    return root, project, marker


def _tail(hook: dict) -> list[str]:
    """The script path and its arguments, after the launcher prefix."""
    args = hook["args"]
    assert tuple(args[: len(UV_LAUNCHER_ARGV)]) == UV_LAUNCHER_ARGV, args
    return list(args[len(UV_LAUNCHER_ARGV):])


def _run(args: list[str], root: Path, project: Path) -> subprocess.CompletedProcess[str]:
    # HOME stays the real one so uv finds its own cache and managed interpreters; the
    # planted "home" only has to be an ancestor of the plugin root, as ~ is in a real
    # install.
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", ""),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(root),
    }
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(project), "session_id": "cwd-test"})
    argv = ["uv", *(a.replace("${CLAUDE_PLUGIN_ROOT}", str(root)) for a in args)]
    return subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=120, env=env, cwd=str(project))


@pytest.fixture(scope="module", autouse=True)
def _require_uv() -> None:
    assert shutil.which("uv"), "uv is not on PATH; it is the launcher under test, so this is a failure, not a skip"


def test_the_walk_finds_the_uv_entries() -> None:
    """Non-vacuity: seven conexus entries and four sn entries launch through uv."""
    counts = {p: sum(1 for plugin, _, _ in _uv_entries() if plugin == p) for p in _PLUGINS}
    assert counts == {"conexus": 7, "sn": 4}, counts


@pytest.mark.parametrize(
    "plugin,event,hook",
    _uv_entries(),
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_a_planted_interpreter_never_runs(plugin: str, event: str, hook: dict, tmp_path: Path) -> None:
    root, project, marker = _layout(plugin, tmp_path)
    proc = _run(hook["args"], root, project)
    assert not marker.exists(), (
        f"{plugin} {event} {hook['args'][-1]}: uv ran a planted interpreter "
        f"({marker.read_text()!r}); stderr={proc.stderr!r}"
    )


def _a_conexus_entry() -> dict:
    return next(h for p, _, h in _uv_entries() if p == "conexus")


def test_the_plant_is_live_when_uv_run_discovers_from_the_project(tmp_path: Path) -> None:
    """Control: ``uv run`` with the project as its discovery origin runs the plant."""
    root, project, marker = _layout("conexus", tmp_path)
    _run([*_RUN_FROM_CWD, *_tail(_a_conexus_entry())], root, project)
    assert marker.exists(), "the plant did not run under `uv run` from the project; it is inert"
    assert str(project) in marker.read_text()


def test_the_plant_is_live_when_uv_run_discovers_from_the_plugin_root(tmp_path: Path) -> None:
    """Control: ``uv run --directory <plugin root>`` still walks up into the home."""
    root, project, marker = _layout("conexus", tmp_path)
    _run([*_RUN_FROM_PLUGIN_ROOT, *_tail(_a_conexus_entry())], root, project)
    assert marker.exists(), "the home plant did not run under `uv run --directory`; it is inert"
    assert str(tmp_path / "home") in marker.read_text()
