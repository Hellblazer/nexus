# SPDX-License-Identifier: AGPL-3.0-or-later
"""``_run_python_hook.sh`` prefers the installed conexus generation's python
(nexus-owna8 follow-up, 2026-09-08).

The hooks that ask the catalog or T3 a question import ``nexus``. On a box
whose PATH python is Homebrew's, that import fails, the failure cannot be
logged, and the rdr hook reported a fully indexed tree as NOT indexed on
every session start; the fix that shipped for it in 7.36.1 was inert in
production because the runner never chose an interpreter that had the
package. These tests drive the real runner with a fake generation root.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "conexus" / "hooks" / "scripts" / "_run_python_hook.sh"


def _fake_generation(tools: Path, marker: str) -> Path:
    gen = tools / "gen-test"
    (gen / "bin").mkdir(parents=True)
    py = gen / "bin" / "python"
    # A stand-in interpreter: prints the marker, then runs the real python on
    # the hook so the exec contract (same argv) is observable end to end.
    py.write_text(f"#!/bin/sh\necho {marker}\nexec {sys.executable} \"$@\"\n")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    (tools / "current").symlink_to(gen)
    return py


def _run(tools: Path, hook: Path) -> subprocess.CompletedProcess:
    # The test's own venv imports nexus and would win over the fake
    # generation by design; these tests pin the generation branch.
    env = {k: v for k, v in os.environ.items() if k not in {"VIRTUAL_ENV", "NX_HOOK_PYTHON"}}
    env["NX_TOOLS_DIR"] = str(tools)
    return subprocess.run(["bash", str(RUNNER), str(hook)], env=env, capture_output=True, text=True, timeout=30)


def test_generation_python_wins_when_current_exists(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    _fake_generation(tools, "GEN-PY-RAN")
    hook = tmp_path / "hook.py"
    hook.write_text("import sys; print('hook ok', sys.argv[0].rsplit('/',1)[-1])\n")
    proc = _run(tools, hook)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["GEN-PY-RAN", "hook ok hook.py"]


def test_falls_back_to_a_named_python_without_a_generation(tmp_path: Path) -> None:
    tools = tmp_path / "no-generation"
    tools.mkdir()
    hook = tmp_path / "hook.py"
    hook.write_text("import sys; print(sys.version_info[:2] >= (3, 12))\n")
    proc = _run(tools, hook)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True"


def test_a_dangling_current_link_is_skipped(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "current").symlink_to(tools / "gen-gone")
    hook = tmp_path / "hook.py"
    hook.write_text("print('fallback ran')\n")
    proc = _run(tools, hook)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "fallback ran"


def test_runner_keeps_the_named_python_probes_after_the_generation_check() -> None:
    text = RUNNER.read_text()
    assert text.index("current/bin/python") < text.index("python3.13") < text.index('exec python3 "$@"')


def test_a_generation_python_that_cannot_run_is_skipped(tmp_path: Path) -> None:
    """A partially reaped or wrong-arch generation: the file is executable
    but does not run. The runner must fall through, not exec into a 127."""
    tools = tmp_path / "tools"
    gen = tools / "gen-broken"
    (gen / "bin").mkdir(parents=True)
    py = gen / "bin" / "python"
    py.write_text("#!/nonexistent/interpreter\n")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    (tools / "current").symlink_to(gen)
    hook = tmp_path / "hook.py"
    hook.write_text("print('fallback ran')\n")
    proc = _run(tools, hook)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "fallback ran"


def test_no_home_and_no_tools_dir_still_runs_the_hook(tmp_path: Path) -> None:
    hook = tmp_path / "hook.py"
    hook.write_text("print('no home ok')\n")
    env = {k: v for k, v in os.environ.items() if k not in {"HOME", "NX_TOOLS_DIR", "VIRTUAL_ENV", "NX_HOOK_PYTHON"}}
    proc = subprocess.run(["bash", str(RUNNER), str(hook)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "no home ok"


def test_explicit_override_wins_over_a_generation(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    _fake_generation(tools, "GEN-PY-RAN")
    override = tmp_path / "chosen"
    override.write_text(f"#!/bin/sh\necho OVERRIDE-RAN\nexec {sys.executable} \"$@\"\n")
    override.chmod(override.stat().st_mode | stat.S_IXUSR)
    hook = tmp_path / "hook.py"
    hook.write_text("print('hook ok')\n")
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env.update(NX_TOOLS_DIR=str(tools), NX_HOOK_PYTHON=str(override))
    proc = subprocess.run(["bash", str(RUNNER), str(hook)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.stdout.splitlines() == ["OVERRIDE-RAN", "hook ok"]


def test_an_active_venv_that_imports_nexus_wins_over_a_generation(tmp_path: Path) -> None:
    """The dev checkout's venv (this test's interpreter imports nexus) beats
    the installed generation, so hooks read the tree being edited."""
    tools = tmp_path / "tools"
    _fake_generation(tools, "GEN-PY-RAN")
    # This test's own venv (sys.prefix) is one whose python imports nexus; a
    # bare symlink elsewhere would lose the venv's site-packages.
    venv = Path(sys.prefix)
    assert (venv / "bin" / "python").exists()
    hook = tmp_path / "hook.py"
    hook.write_text("print('hook ok')\n")
    env = {k: v for k, v in os.environ.items() if k != "NX_HOOK_PYTHON"}
    env.update(NX_TOOLS_DIR=str(tools), VIRTUAL_ENV=str(venv))
    proc = subprocess.run(["bash", str(RUNNER), str(hook)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["hook ok"]


def test_a_venv_without_nexus_does_not_win(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    _fake_generation(tools, "GEN-PY-RAN")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    bare = venv / "bin" / "python"
    # Runnable (so `-c ''` succeeds) but cannot import nexus: a weakened
    # probe that only checks runnability would wrongly pick it.
    bare.write_text("#!/bin/sh\ncase \"$*\" in *nexus*) exit 1;; esac\nexit 0\n")
    bare.chmod(bare.stat().st_mode | stat.S_IXUSR)
    hook = tmp_path / "hook.py"
    hook.write_text("print('hook ok')\n")
    env = {k: v for k, v in os.environ.items() if k != "NX_HOOK_PYTHON"}
    env.update(NX_TOOLS_DIR=str(tools), VIRTUAL_ENV=str(venv))
    proc = subprocess.run(["bash", str(RUNNER), str(hook)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.stdout.splitlines() == ["GEN-PY-RAN", "hook ok"]
