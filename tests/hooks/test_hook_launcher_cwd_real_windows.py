# SPDX-License-Identifier: AGPL-3.0-or-later
"""A uv-launched hook never runs a ``.venv`` interpreter it did not ask for, on
real Windows (RDR-224 test review S5, nexus-f9bgu.36 finding C).

``tests/hooks/test_hook_launcher_cwd.py`` proves this on POSIX and skips its whole
module on Windows, for one reason: its plants are ``#!/bin/sh`` scripts under
``.venv/bin``, and a Windows venv keeps ``Scripts\\python.exe``, which cannot run a
shebang file. On Windows its never-runs cases would pass vacuously and its two
controls fail. The Windows evidence for the fix was one hand run in a real Claude
Code session.

This file is the portable variant. The plant is what ``tests/test_mcpb_launcher_venv.py``
already ran on real Windows: a real ``uv venv`` whose ``site-packages`` carries a
``.pth`` that appends to a marker file when that venv's interpreter starts, so the
marker exists exactly when uv ran the planted interpreter. It runs the same real argv
from ``conexus/hooks/hooks.json`` and ``sn/hooks/hooks.json`` (the plugin copied under
a fake home, the project as the process cwd, plants in the project, its parent and
the home above the plugin root), and the same two controls: the plant is live under
``uv run`` from the project and under ``uv run --directory <plugin root>``. The
controls are what make "the marker is absent" mean something.

One addition the POSIX file lacks: its never-runs cases assert only that no marker
appeared, which a launcher that failed before running anything would also satisfy.
``test_the_launcher_still_ran_the_script_with_the_plants_in_place`` runs the
credential guard through the same layout and requires its deny verdict.

The layout is built once per module (three ``uv venv`` calls, not thirty-three).

Windows only, and in the rehearsal's Windows test set. The class is a
``--require-passed`` pattern in ``windows-pg-bundle-rehearsal.yml``, so a skip fails
that job instead of passing it having proved nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._hook_wiring import REPO_ROOT
from tests.hooks.test_hook_launcher_cwd import (
    _PLUGINS,
    _RUN_FROM_CWD,
    _RUN_FROM_PLUGIN_ROOT,
    _a_conexus_entry,
    _tail,
    _uv_entries,
)
from tests.hooks.test_hook_launcher_uv import _CRED_READ, _hook_env

ON_WINDOWS = sys.platform == "win32"


def _site_packages(venv: Path) -> Path:
    found = list(venv.glob("lib/python*/site-packages")) + list(venv.glob("Lib/site-packages"))
    assert len(found) == 1, found
    return found[0]


def _plant(directory: Path, marker: Path) -> None:
    """A real venv in *directory* whose interpreter appends to *marker* on every start."""
    venv = directory / ".venv"
    uv = shutil.which("uv")
    assert uv, "uv is not on PATH; it is the launcher under test, so this is a failure, not a skip"
    subprocess.run(
        [uv, "venv", "--quiet", "--python", sys.executable, str(venv)],
        check=True, env=_hook_env(os.environ, sys.platform), timeout=300,
    )
    (_site_packages(venv) / "zz_plant.pth").write_text(
        f"import os; open({str(marker)!r}, 'a').write('ran from ' + {str(directory)!r} + chr(10))\n", encoding="utf-8"
    )


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Both plugins' hook trees installed under a planted home, and a planted project
    (and parent) outside it. ``marker`` is shared; a test clears it first."""
    base = tmp_path_factory.mktemp("hook-launcher-cwd")
    marker = base / "marker"
    home = base / "home"
    roots = {}
    for plugin in _PLUGINS:
        root = home / ".claude" / "plugins" / "cache" / "nexus-plugins" / plugin / "0.0.0"
        shutil.copytree(REPO_ROOT / plugin / "hooks", root / "hooks")
        if (REPO_ROOT / plugin / "launch").is_dir():
            # RDR-228: seven conexus entries start `launch/nx_launch.py`.
            shutil.copytree(REPO_ROOT / plugin / "launch", root / "launch")
        roots[plugin] = root
    outer = base / "outer"
    project = outer / "project"
    project.mkdir(parents=True)
    for d in (home, project, outer):
        _plant(d, marker)
    return SimpleNamespace(marker=marker, roots=roots, project=project, home=home, state=base / "lockstep-state")


@pytest.fixture()
def clean(world: SimpleNamespace) -> SimpleNamespace:
    world.marker.unlink(missing_ok=True)
    return world


def _run(
    world: SimpleNamespace, args: list[str], plugin: str, payload: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    root = world.roots[plugin]
    # The host's environment minus the interpreter-selection variables, as a hook has it
    # (test_hook_launcher_uv._hook_env); the planted "home" only has to be an ancestor of
    # the plugin root, as ~ is in a real install. The four NX_ paths point the version-lockstep
    # entry at an empty state of its own, so running it here can never read this machine's
    # plugin registry and dispatch a real `nx upgrade`.
    env = {
        **_hook_env(os.environ, sys.platform),
        "CLAUDE_PROJECT_DIR": str(world.project),
        "CLAUDE_PLUGIN_ROOT": str(root),
        "NX_PLUGIN_REGISTRY": str(world.state / "installed_plugins.json"),
        "NX_PLUGIN_MARKETPLACES": str(world.state / "known_marketplaces.json"),
        "NX_LOCKSTEP_MARKER": str(world.state / "marker"),
        "NX_LOCKSTEP_REF_DRIFT_MARKER": str(world.state / "ref-drift-marker"),
    }
    body = payload or {
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "cwd-test",
    }
    uv = shutil.which("uv") or "uv"
    if any(a.endswith("/launch/nx_launch.py") for a in args):
        # A launcher entry would find this machine's own install and run its real
        # hook verbs (an upgrade, a generation sweep). This file is about which
        # interpreter uv picks, so the launcher gets no generation layout and a PATH
        # with every directory that holds a Nexus hook program removed: it starts,
        # finds no CLI and exits 0. uv is named by absolute path, so it needs no PATH.
        env["NX_TOOLS_DIR"] = str(world.state / "no-tools")
        env["PATH"] = os.pathsep.join(
            entry for entry in env.get("PATH", "").split(os.pathsep)
            if entry and not any(
                (Path(entry) / name).exists()
                for name in ("nx-hook", "nx-hook.exe", "nx-session-end-launcher", "nx-session-end-launcher.exe")
            )
        )
    argv = [uv, *(a.replace("${CLAUDE_PLUGIN_ROOT}", str(root)) for a in args)]
    return subprocess.run(
        argv, input=json.dumps({**body, "cwd": str(world.project)}), capture_output=True, text=True,
        encoding="utf-8", timeout=180, env=env, cwd=str(world.project),
    )


@pytest.mark.skipif(not ON_WINDOWS, reason="needs a real Windows venv layout and uv's Windows interpreter discovery")
class TestRealWindows:
    def test_the_walk_finds_the_uv_entries(self) -> None:
        """Non-vacuity: fifteen conexus entries and four sn entries launch through uv
        (7 until RDR-228, nexus-sv2wx.8, put seven entries behind the launcher and
        added the setup notice hook)."""
        counts = {p: sum(1 for plugin, _, _ in _uv_entries() if plugin == p) for p in _PLUGINS}
        assert counts == {"conexus": 15, "sn": 4}, counts

    def test_a_launcher_entry_really_starts_the_launcher(self, clean: SimpleNamespace) -> None:
        """The never-runs cases prove nothing about an entry whose script did not
        start. With no CLI the launcher writes its one line, which shows uv ran it."""
        hook = next(h for p, _, h in _uv_entries() if p == "conexus" and "nx-hook" in h["args"])
        proc = _run(clean, hook["args"], "conexus")
        assert proc.returncode == 0 and "`nx-hook` is not installed" in proc.stderr, proc
        assert not clean.marker.exists()

    @pytest.mark.parametrize(
        "plugin,event,hook",
        _uv_entries(),
        ids=lambda v: v if isinstance(v, str) else None,
    )
    def test_a_planted_interpreter_never_runs(
        self, plugin: str, event: str, hook: dict, clean: SimpleNamespace,
    ) -> None:
        proc = _run(clean, hook["args"], plugin)
        planted = clean.marker.read_text(encoding="utf-8") if clean.marker.exists() else ""
        assert planted == "", (
            f"{plugin} {event} {hook['args'][-1]}: uv ran a planted interpreter ({planted!r}); stderr={proc.stderr!r}"
        )

    def test_the_launcher_still_ran_the_script_with_the_plants_in_place(self, clean: SimpleNamespace) -> None:
        """A launcher that failed before running anything would leave no marker either. The
        credential guard answers a deny for this payload, so the script demonstrably ran."""
        (hook,) = [h for p, _, h in _uv_entries() if p == "conexus" and h["args"][-1].endswith("credential_print_guard.py")]
        proc = _run(clean, hook["args"], "conexus", payload=_CRED_READ)
        assert proc.returncode == 0, proc.stderr
        verdict = json.loads(proc.stdout)["hookSpecificOutput"]
        assert verdict["permissionDecision"] == "deny", proc.stdout
        assert not clean.marker.exists(), clean.marker.read_text(encoding="utf-8")

    def test_the_plant_is_live_when_uv_run_discovers_from_the_project(self, clean: SimpleNamespace) -> None:
        """Control: ``uv run`` with the project as its discovery origin runs the plant."""
        _run(clean, [*_RUN_FROM_CWD, *_tail(_a_conexus_entry())], "conexus")
        assert clean.marker.exists(), "the plant did not run under `uv run` from the project; it is inert"
        assert str(clean.project) in clean.marker.read_text(encoding="utf-8")

    def test_the_plant_is_live_when_uv_run_discovers_from_the_plugin_root(self, clean: SimpleNamespace) -> None:
        """Control: ``uv run --directory <plugin root>`` still walks up into the home."""
        _run(clean, [*_RUN_FROM_PLUGIN_ROOT, *_tail(_a_conexus_entry())], "conexus")
        assert clean.marker.exists(), "the home plant did not run under `uv run --directory`; it is inert"
        assert str(clean.home) in clean.marker.read_text(encoding="utf-8")
