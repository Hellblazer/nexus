# SPDX-License-Identifier: AGPL-3.0-or-later
"""A uv-launched hook never runs an interpreter the project directory supplies.

RDR-224 review, finding C (nexus-f9bgu.36). Claude Code runs a hook with the
project as its cwd. ``uv run --no-project --no-config <script>`` still searches
the cwd and its parents for a virtual environment while it looks for an
interpreter, and it EXECUTES what it finds there to read the version: a
``.venv/bin/python`` committed to a cloned repository ran on every uv-launched
hook, on every platform (measured on macOS, uv 0.8.0; it ran under ``--python``,
``--python-preference`` and ``--managed-python`` too). ``python3`` as a launcher
never had this: it is the PATH's.

The cure is ``--directory ${CLAUDE_PLUGIN_ROOT}`` in every uv entry: uv changes
into the plugin root before it discovers anything. The scripts then run with the
plugin root as their process cwd, so they take the project directory from the
payload's ``cwd`` (or ``CLAUDE_PROJECT_DIR``), never from the process.

Each case runs the real argv from hooks.json, exec form, with the project as cwd,
the plugin root substituted, and a marker-writing interpreter planted in the
project and in its parent. ``test_the_plant_is_live_without_the_directory_flag``
is the non-vacuity control: the same plant, the same entry, minus the flag, and
the marker appears.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._hook_wiring import REPO_ROOT

_PLUGINS = ("conexus", "sn")
_NAMES = ("python", "python3", "python3.12", "python3.13")


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


def _argv(plugin: str, hook: dict, *, with_directory: bool = True) -> list[str]:
    root = str(REPO_ROOT / plugin)
    args = [a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in hook["args"]]
    if not with_directory:
        i = args.index("--directory")
        del args[i : i + 2]
    return ["uv", *args]


def _run(plugin: str, hook: dict, tmp_path: Path, *, with_directory: bool = True) -> tuple[Path, subprocess.CompletedProcess[str]]:
    outer = tmp_path / "outer"
    project = outer / "project"
    project.mkdir(parents=True)
    marker = tmp_path / "marker"
    _plant(project, marker)
    _plant(outer, marker)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", ""),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT / plugin),
    }
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(project), "session_id": "cwd-test"})
    proc = subprocess.run(
        _argv(plugin, hook, with_directory=with_directory), input=payload,
        capture_output=True, text=True, timeout=120, env=env, cwd=str(project),
    )
    return marker, proc


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
def test_a_planted_interpreter_in_the_project_never_runs(plugin: str, event: str, hook: dict, tmp_path: Path) -> None:
    marker, proc = _run(plugin, hook, tmp_path)
    assert not marker.exists(), (
        f"{plugin} {event} {hook['args'][-1]}: uv ran an interpreter planted in the project "
        f"({marker.read_text()!r}); stderr={proc.stderr!r}"
    )


def test_the_plant_is_live_without_the_directory_flag(tmp_path: Path) -> None:
    """Control: remove ``--directory`` from a real entry and the planted interpreter runs,
    so the cases above are not passing because the plant is inert."""
    plugin, _, hook = next(e for e in _uv_entries() if e[0] == "conexus")
    marker, _ = _run(plugin, hook, tmp_path, with_directory=False)
    assert marker.exists(), "the planted interpreter did not run even without the flag; the plant is inert"
