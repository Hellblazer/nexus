# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desktop extension never runs an interpreter from a ``.venv`` it did not create.

nexus-92gxf. Claude Desktop starts the extension with ``mcp_config`` from
``mcpb/manifest.json``, with ``${__dirname}`` set to the unpacked bundle. The
bundle sits under the user's home (``~/Library/Application Support/Claude/Claude
Extensions/<id>`` on macOS, ``%APPDATA%\\Claude\\Claude Extensions\\<id>`` on
Windows). Two ways an ancestor of that directory used to supply the interpreter:

* ``uv run --no-project --directory ${__dirname}`` searches the bundle directory
  and every parent for a ``.venv`` and runs the bootstrap with the first one it
  finds: ``~/.venv``, or ``C:\\.venv``, which any local Windows user may create.
  The launcher is now ``uv tool run ... --python >=3.12 python``; a tool
  environment is built from a managed or PATH interpreter, never a ``.venv``.
* The bootstrap then runs ``uv sync`` and ``uv run`` in the bundle as a project.
  uv's workspace discovery walks up too: a ``pyproject.toml`` in an ancestor
  whose ``[tool.uv.workspace]`` members match the bundle makes that ancestor the
  workspace root, and its ``.venv`` becomes the server's environment. The bundle
  declares itself a workspace root (``[tool.uv.workspace] members = []``), which
  stops the walk.

Each case copies the real manifest and bootstrap into a fake home, swaps the
bundle's dependencies for none (so ``uv sync`` is local and fast) and its server
for a stub that records ``sys.executable``, plants a real ``uv venv`` with a
marker-writing ``.pth`` in the cwd, the bundle's parent and the home, and runs
the manifest's own argv with ``${__dirname}`` substituted. The two controls show
each plant is live under the form it defeats.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCPB = REPO_ROOT / "mcpb"

#: The pre-92gxf launcher, kept as the control's argv.
_OLD_LAUNCHER = ["run", "--no-project", "--directory", "${__dirname}", "src/bootstrap.py"]


@pytest.fixture(scope="module", autouse=True)
def _require_uv() -> None:
    assert shutil.which("uv"), "uv is not on PATH; it is the launcher under test, so this is a failure, not a skip"


def _site_packages(venv: Path) -> Path:
    found = list(venv.glob("lib/python*/site-packages")) + list(venv.glob("Lib/site-packages"))
    assert len(found) == 1, found
    return found[0]


def _plant(directory: Path, marker: Path) -> None:
    """A real venv whose interpreter appends to *marker* on every start."""
    venv = directory / ".venv"
    subprocess.run(["uv", "venv", "--quiet", "--python", sys.executable, str(venv)], check=True, env=_env())
    (_site_packages(venv) / "zz_plant.pth").write_text(
        f"import os; open({str(marker)!r}, 'a').write('ran from ' + {str(directory)!r} + chr(10))\n"
    )


def _env() -> dict[str, str]:
    # No VIRTUAL_ENV: Desktop does not set one. HOME stays real so uv finds its
    # cache and managed interpreters; the planted home only has to be an ancestor
    # of the bundle, as it is in a real install.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "VIRTUAL_ENV", "NX_", "PYTHON"))}
    return env


def _stub_pyproject() -> str:
    """The real bundle pyproject with its dependency list emptied."""
    text = (MCPB / "pyproject.toml").read_text()
    stubbed, n = re.subn(r"(?ms)^dependencies = \[.*?^\]\n", "dependencies = []\n", text)
    assert n == 1, "could not find the bundle's dependencies block"
    return stubbed


def _layout(tmp_path: Path, *, pyproject: str | None = None) -> tuple[Path, Path, Path, Path]:
    """``(bundle, cwd, home, marker)`` with the bundle installed under a planted home."""
    marker = tmp_path / "marker"
    home = tmp_path / "home"
    bundle = home / "Library" / "Application Support" / "Claude" / "Claude Extensions" / "local.mcpb.test.conexus"
    (bundle / "src").mkdir(parents=True)
    shutil.copy2(MCPB / "manifest.json", bundle / "manifest.json")
    shutil.copy2(MCPB / "src" / "bootstrap.py", bundle / "src" / "bootstrap.py")
    (bundle / "pyproject.toml").write_text(pyproject if pyproject is not None else _stub_pyproject())
    (bundle / "src" / "server.py").write_text(
        f"import sys; open({str(tmp_path / 'server-ran')!r}, 'w').write(sys.executable)\n"
    )
    # An ancestor workspace that claims the extensions directory; its .venv is the
    # home plant below.
    (home / "pyproject.toml").write_text(
        '[project]\nname = "ancestor"\nversion = "0"\nrequires-python = ">=3.12"\n'
        '[tool.uv.workspace]\nmembers = ["Library/Application Support/Claude/Claude Extensions/*"]\n'
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    for d in (cwd, bundle.parent, home):
        _plant(d, marker)
    return bundle, cwd, home, marker


def _launch(args: list[str], bundle: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    argv = ["uv", *(a.replace("${__dirname}", str(bundle)) for a in args)]
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, env=_env(), cwd=str(cwd))


def _manifest_args() -> list[str]:
    cfg = json.loads((MCPB / "manifest.json").read_text())["server"]["mcp_config"]
    assert cfg["command"] == "uv"
    return cfg["args"]


def test_the_manifest_launch_runs_no_planted_interpreter(tmp_path: Path) -> None:
    bundle, cwd, _, marker = _layout(tmp_path)
    proc = _launch(_manifest_args(), bundle, cwd)
    planted = marker.read_text() if marker.exists() else ""
    assert planted == "", f"a planted interpreter ran:\n{planted}stderr={proc.stderr!r}"
    ran = tmp_path / "server-ran"
    assert ran.exists(), f"the stub server never ran: rc={proc.returncode} stderr={proc.stderr!r}"
    assert Path(ran.read_text()).parent.parent == bundle / ".venv", ran.read_text()


def test_control_the_old_launcher_runs_the_bundle_parent_plant(tmp_path: Path) -> None:
    """``uv run --no-project --directory ${__dirname}`` walks up into the plants."""
    bundle, cwd, _, marker = _layout(tmp_path)
    _launch(_OLD_LAUNCHER, bundle, cwd)
    assert marker.exists(), "the old launcher ran no plant; the plants are inert"
    assert str(bundle.parent) in marker.read_text()


def test_control_without_its_own_workspace_root_the_server_runs_from_the_ancestor(tmp_path: Path) -> None:
    """Drop the bundle's ``[tool.uv.workspace]`` and the ancestor workspace captures it."""
    stub = _stub_pyproject()
    without, n = re.subn(r"(?ms)^\[tool\.uv\.workspace\]\n.*?(?=^\[|\Z)", "", stub)
    assert n == 1, "the bundle pyproject declares no [tool.uv.workspace]"
    bundle, cwd, home, marker = _layout(tmp_path, pyproject=without)
    _launch(_manifest_args(), bundle, cwd)
    assert marker.exists() and str(home) in marker.read_text(), "the ancestor workspace did not capture the bundle; the plant is inert"
