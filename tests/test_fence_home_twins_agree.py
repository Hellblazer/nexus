# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The shell and Python HOME fences must produce the same shape.

nexus-pfuns. Two implementations exist deliberately: ``tests/e2e/lib/
fence_home.sh`` for the gate scripts (which run before ``uv sync`` on some
paths and cannot depend on a Python import) and ``tests/_fence_home.py`` for the
unit suite. Two copies of one rule drift until the stale one wins an argument it
should not, so this pins the contract rather than trusting the comments.

The rule: mirror EVERY top-level entry of the real home, recreate ``.config``
as a real directory mirroring its own entries, and shadow exactly
``.config/nexus`` as a fresh empty directory.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests._fence_home import fence_home

_SHELL_FENCE = Path(__file__).parent / "e2e" / "lib" / "fence_home.sh"


def _seed(real: Path) -> None:
    """A home containing the entries that actually mattered on 2026-08-24."""
    for rel in (".docker/run", ".m2/repository", ".cache/uv", ".local/bin",
                ".claude/plugins", ".config/nexus", ".config/gh", "Documents"):
        (real / rel).mkdir(parents=True, exist_ok=True)
    (real / ".testcontainers.properties").write_text("testcontainers.ryuk.disabled=true\n")
    (real / ".config" / "nexus" / "last_seen_version").write_text("7.18.0\n")


def _shape(home: Path) -> set[str]:
    """(name, is-symlink) for the mirror's top level plus its .config level."""
    out = set()
    for p in sorted(home.iterdir()):
        out.add(f"{p.name}:{'link' if p.is_symlink() else 'dir'}")
    cfg = home / ".config"
    if cfg.is_dir():
        for p in sorted(cfg.iterdir()):
            out.add(f".config/{p.name}:{'link' if p.is_symlink() else 'dir'}")
    return out


@pytest.mark.skipif(not _SHELL_FENCE.exists(), reason="fence_home.sh missing")
def test_shell_and_python_fences_produce_identical_shapes(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    _seed(real)

    py_home = tmp_path / "py"
    fence_home(real, py_home, ".config/nexus")

    sh_home = tmp_path / "sh"
    r = subprocess.run(
        ["bash", "-c", f'source "{_SHELL_FENCE}"; fence_home "{real}" "{sh_home}" ".config/nexus"'],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"shell fence failed: {r.stderr}"

    py, sh = _shape(py_home), _shape(sh_home)
    assert py == sh, (
        "the two fence implementations have drifted:\n"
        f"  python only: {sorted(py - sh)}\n"
        f"  shell only:  {sorted(sh - py)}"
    )


def test_python_fence_shadows_only_the_nexus_config(tmp_path: Path) -> None:
    real = tmp_path / "real"; real.mkdir(); _seed(real)
    home = fence_home(real, tmp_path / "fenced", ".config/nexus")

    # the shadowed leaf is a REAL empty dir, not a passthrough
    shadowed = home / ".config" / "nexus"
    assert shadowed.is_dir() and not shadowed.is_symlink()
    assert not (shadowed / "last_seen_version").exists()
    # everything else passes through
    for entry in (".docker", ".testcontainers.properties", ".m2", ".cache",
                  ".local", ".claude", "Documents", ".config/gh"):
        assert (home / entry).exists(), f"{entry} did not survive the mirror"


def test_install_fence_is_idempotent_and_records_the_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An xdist worker inherits the fenced HOME. A second fence there would
    mirror the MIRROR, and the guard would lose the operator's real path."""
    from tests._fence_home import REAL_HOME_ENV, install_fence

    real = tmp_path / "real"; real.mkdir(); _seed(real)
    monkeypatch.setenv("HOME", str(real))
    monkeypatch.delenv(REAL_HOME_ENV, raising=False)

    first = install_fence(tmp_path / "f1")
    assert first is not None
    assert Path(os.environ[REAL_HOME_ENV]).resolve() == real.resolve()
    assert Path(os.environ["HOME"]) == tmp_path / "f1"

    second = install_fence(tmp_path / "f2")
    assert second is None, "a second fence installed over an already-fenced HOME"
    assert Path(os.environ["HOME"]) == tmp_path / "f1", "HOME was re-pointed"


import os  # noqa: E402 — used by the idempotence test above
