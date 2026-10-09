# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A fenced HOME must not leak into the venv uv builds for the checkout.

nexus-t0pke. ``fence_home`` recreates ``~/.local`` as a real directory (it
shadows ``.local/state``) whose entries link through to the real ones, so uv
finds its managed Pythons at ``<gate_home>/.local/share/uv/python``. In a
checkout with no ``.venv`` (a fresh worktree), the gate's first ``uv run``
built ``.venv`` on that path: ``bin/python`` and ``pyvenv.cfg``'s ``home =``
pointed into the gate's scratch dir. The gate deleted the scratch dir on exit,
and the next ``uv build --wheel`` under that ``VIRTUAL_ENV``
(``tests/test_tables_lint.py::test_lifecycle_table_present_in_built_wheel``)
failed on the dangling interpreter.

``uv python dir`` prints the managed-Python root uv discovers from, so it is
the cheap, deterministic probe: no managed Python has to exist on the host.
These tests drive the REAL ``tests/e2e/lib/fence_home.sh`` and the real
``tests._fence_home`` twin.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).parent.parent
_GATE = _REPO / "tests" / "e2e" / "local-service-gate.sh"
_FENCE_LIB = _REPO / "tests" / "e2e" / "lib" / "fence_home.sh"
_UV_KEYS = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")


def _base_env() -> dict[str, str]:
    """The ambient env minus every input that would pick uv's roots for it."""
    env = dict(os.environ)
    for k in (*_UV_KEYS, "XDG_DATA_HOME"):
        env.pop(k, None)
    return env


def _gate_sequence(real: Path, gate: Path, *, pin: bool, extra: dict[str, str] | None = None):
    """The gate's own fence sequence, then ``uv python dir`` under it."""
    assert shutil.which("uv"), "uv is not on PATH; this test probes uv's own resolution"
    env = _base_env()
    env.update(extra or {})
    script = (
        f'source "{_FENCE_LIB}"; REAL_HOME="{real}"; '
        f'fence_home "$REAL_HOME" "{gate}" ".config/nexus"; '
        f'export HOME="{gate}"; fence_home_env "{gate}"; '
        + ('fence_uv_env "$REAL_HOME"; ' if pin else "")
        + 'printf "UV_CACHE_DIR=%s\\n" "${UV_CACHE_DIR:-}"; '
        'printf "UV_PYTHON_INSTALL_DIR=%s\\n" "${UV_PYTHON_INSTALL_DIR:-}"; '
        'printf "PYDIR=%s\\n" "$(uv python dir)"'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines())


@pytest.fixture()
def homes(tmp_path: Path) -> tuple[Path, Path]:
    real = tmp_path / "real"
    (real / ".local" / "share" / "uv" / "python").mkdir(parents=True)
    (real / ".local" / "state").mkdir(parents=True)
    (real / ".cache" / "uv").mkdir(parents=True)
    return real, tmp_path / "gate"


def test_unpinned_uv_resolves_managed_pythons_inside_the_fenced_home(homes) -> None:
    """The premise. If uv stopped deriving the root from HOME, the pin below
    would guard nothing and its test would pass for the wrong reason."""
    real, gate = homes
    out = _gate_sequence(real, gate, pin=False)
    assert Path(out["PYDIR"]) == gate / ".local" / "share" / "uv" / "python"


def test_the_gate_fence_pins_uv_pythons_to_the_real_home(homes) -> None:
    real, gate = homes
    out = _gate_sequence(real, gate, pin=True)
    assert Path(out["PYDIR"]) == real / ".local" / "share" / "uv" / "python"
    assert not out["PYDIR"].startswith(str(gate)), out["PYDIR"]
    assert Path(out["UV_CACHE_DIR"]) == real / ".cache" / "uv"


def test_xdg_data_home_and_an_explicit_value_are_honoured(homes, tmp_path: Path) -> None:
    real, gate = homes
    data = tmp_path / "xdg-data"
    out = _gate_sequence(real, gate, pin=True, extra={"XDG_DATA_HOME": str(data)})
    assert Path(out["PYDIR"]) == data / "uv" / "python"

    explicit = tmp_path / "explicit-pythons"
    out = _gate_sequence(real, gate, pin=True, extra={"UV_PYTHON_INSTALL_DIR": str(explicit)})
    assert Path(out["PYDIR"]) == explicit


@pytest.mark.parametrize(
    "outer",
    [{}, {"XDG_DATA_HOME": "/xdg/data"}, {"UV_PYTHON_INSTALL_DIR": "/p", "UV_CACHE_DIR": "/c"},
     {"UV_PYTHON_INSTALL_DIR": "", "UV_CACHE_DIR": ""}],
)
def test_the_python_twin_agrees_with_the_shell_fence(outer: dict[str, str], tmp_path: Path) -> None:
    from tests._fence_home import fence_uv_env

    real = tmp_path / "real"
    with patch.dict(os.environ, clear=False):
        for k in (*_UV_KEYS, "XDG_DATA_HOME"):
            os.environ.pop(k, None)
        os.environ.update(outer)
        fence_uv_env(real)
        py = {k: os.environ.get(k) for k in _UV_KEYS}

    env = _base_env()
    env.update(outer)
    script = (
        f'source "{_FENCE_LIB}"; fence_uv_env "{real}"; '
        'printf "UV_CACHE_DIR=%s\\nUV_PYTHON_INSTALL_DIR=%s\\n" "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR"'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    sh = dict(line.split("=", 1) for line in r.stdout.splitlines())
    assert py == sh
    assert all(py.values()), py


def test_install_fence_applies_the_uv_pin(tmp_path: Path) -> None:
    """The suite's own fence must call the twin, not only define it."""
    from tests._fence_home import FENCED_HOME_ENV, REAL_HOME_ENV, install_fence

    with patch.dict(os.environ, clear=False):
        for k in (*_UV_KEYS, "XDG_DATA_HOME", REAL_HOME_ENV, FENCED_HOME_ENV,
                  "NX_TOOLS_DIR", "NX_BIN_DIR", "UV_TOOL_DIR"):
            os.environ.pop(k, None)
        real = Path(os.path.expanduser("~")).resolve()
        install_fence(tmp_path / "suite-home")
        pydir = os.environ.get("UV_PYTHON_INSTALL_DIR")
    assert pydir == str(real / ".local" / "share" / "uv" / "python")


def test_the_gate_pins_uv_before_its_first_uv_call() -> None:
    """Every test above drives the helpers; this is the one that notices if the
    GATE stops calling them, or calls them too late."""
    body = _GATE.read_text()
    call = 'fence_uv_env "$REAL_HOME"'
    assert call in body, "the gate no longer pins uv's roots to the real home"
    assert body.index('export HOME="$GATE_HOME"') < body.index(call)
    assert body.index(call) < body.index("uv run "), "a uv call runs before the pin"
