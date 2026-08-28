# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""One rule resolves uv's tool directory (nexus-orhp5).

FOUR rules used to answer "where is uv's tool dir", and they disagreed:

  1. ``upgrade_finish.running_from_tool_install`` — substring test for
     ``"uv/tools/conexus"``. Wrong under a relocated ``UV_TOOL_DIR``.
  2. ``health._check_orphan_uv_install`` — honoured ``UV_TOOL_DIR`` but not
     ``XDG_DATA_HOME``.
  3. ``legacy.sh`` / ``version_lockstep_action`` — shell out to ``uv tool
     dir``. Correct by construction; left alone, they ARE the reference.
  4. ``upgrade_finish``'s uv-receipt read — a hardcoded
     ``~/.local/share/uv/tools/...``, wrong for both env vars. Found by the
     sweep the bead asked for; "three rules" was an undercount.

Rule 1 stopped being harmless when nexus-gu9zo made it route install
convergence: a ``UV_TOOL_DIR``-relocated box got the dev-checkout refusal,
which is the misdirection gu9zo existed to remove, surviving for a subset.

EVERY TEST HERE TARGETS A SPECIFIC OLD RULE'S BLIND SPOT, so each one fails
against the implementation it replaced rather than merely passing against the
new one. ``test_python_resolution_agrees_with_uv_itself`` is the load-bearing
one: it binds the Python rule to rule 3's shell answer, which is the only
reference that cannot drift from uv by construction.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nexus.install_layout import (
    is_under_uv_tool_install,
    uv_conexus_venv,
    uv_tool_root,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


# ── precedence, measured against uv 0.8.0 ──────────────────────────────────

def test_default_is_the_xdg_style_home_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert uv_tool_root() == tmp_path / ".local" / "share" / "uv" / "tools"


def test_uv_tool_dir_relocates_it(monkeypatch, tmp_path):
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "elsewhere"))
    assert uv_tool_root() == tmp_path / "elsewhere"


def test_xdg_data_home_relocates_it(monkeypatch, tmp_path):
    """OLD RULE 2's BLIND SPOT: health honoured UV_TOOL_DIR only."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert uv_tool_root() == tmp_path / "xdg" / "uv" / "tools"


def test_uv_tool_dir_wins_over_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "win"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "lose"))
    assert uv_tool_root() == tmp_path / "win"


def test_empty_env_values_are_treated_as_unset(monkeypatch, tmp_path):
    """``Path("")`` is ``Path(".")`` — honouring an exported-but-empty var
    would root the uv tree at the caller's CWD."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("UV_TOOL_DIR", "   ")
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert uv_tool_root() == tmp_path / ".local" / "share" / "uv" / "tools"


# ── the reference: agree with uv, which cannot drift from itself ───────────

def test_python_resolution_agrees_with_uv_itself(monkeypatch, tmp_path):
    """Bind rules 1/2/4 to rule 3's answer.

    NOT skip-guarded on uv's absence. The suite runs under ``uv run``, and CI
    installs uv before invoking pytest, so uv IS on PATH by construction — a
    skip here would be the vacuous-gate shape this repo has already paid for.
    If this fails because uv vanished, that is worth knowing loudly.
    """
    uv = shutil.which("uv")
    assert uv, "uv is not on PATH; this suite runs under `uv run`, so that is a real problem"

    for env in (
        {},
        {"UV_TOOL_DIR": str(tmp_path / "a")},
        {"XDG_DATA_HOME": str(tmp_path / "b")},
        {"UV_TOOL_DIR": str(tmp_path / "c"), "XDG_DATA_HOME": str(tmp_path / "d")},
    ):
        merged = {**os.environ}
        merged.pop("UV_TOOL_DIR", None)
        merged.pop("XDG_DATA_HOME", None)
        merged.update(env)
        proc = subprocess.run(
            [uv, "tool", "dir"], capture_output=True, text=True, env=merged, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        for k in ("UV_TOOL_DIR", "XDG_DATA_HOME"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        assert uv_tool_root() == Path(proc.stdout.strip()), (
            f"python and `uv tool dir` disagree under {env!r}"
        )


# ── containment, not substring ─────────────────────────────────────────────

def test_containment_finds_the_tree_under_a_relocated_uv_tool_dir(monkeypatch, tmp_path):
    """OLD RULE 1's BLIND SPOT, and the one that routed install convergence.

    The substring ``"uv/tools/conexus" in str(root)`` answers NO here, because
    a relocated UV_TOOL_DIR need not spell those segments at all.
    """
    root = tmp_path / "somewhere" / "totally" / "else"
    monkeypatch.setenv("UV_TOOL_DIR", str(root))
    site = root / "conexus" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)

    assert "uv/tools/conexus" not in str(site), "fixture no longer tests the blind spot"
    assert is_under_uv_tool_install(site) is True


def test_containment_rejects_a_path_that_merely_spells_the_segments(monkeypatch, tmp_path):
    """The other half a substring got wrong: a false YES."""
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "real"))
    (tmp_path / "real" / "conexus").mkdir(parents=True)
    decoy = tmp_path / "decoy" / "uv" / "tools" / "conexus" / "lib"
    decoy.mkdir(parents=True)

    assert "uv/tools/conexus" in str(decoy), "fixture no longer tests the blind spot"
    assert is_under_uv_tool_install(decoy) is False


def test_the_venv_root_itself_counts_as_inside(monkeypatch, tmp_path):
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "t"))
    (tmp_path / "t" / "conexus").mkdir(parents=True)
    assert is_under_uv_tool_install(uv_conexus_venv()) is True


# ── the rewired call sites follow the one rule ─────────────────────────────

def test_running_from_tool_install_sees_a_relocated_tree(monkeypatch, tmp_path):
    """Rule 1's site, end to end."""
    import nexus.upgrade_finish as uf

    root = tmp_path / "relocated"
    monkeypatch.setenv("UV_TOOL_DIR", str(root))
    site = root / "conexus" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    monkeypatch.setattr(uf, "_install_root", lambda: site)

    assert uf.running_from_tool_install() is True


def test_orphan_uv_check_sees_an_xdg_relocated_tree(monkeypatch, tmp_path):
    """Rule 2's site. Before, an XDG-relocated box reported 'no uv install'
    while one sat right there."""
    from nexus.health import _check_orphan_uv_install

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "uv" / "tools" / "conexus" / "bin").mkdir(parents=True)

    results = _check_orphan_uv_install()
    assert len(results) == 1
    assert results[0].ok is False, "the orphan uv install was not detected"
    assert "conexus" in results[0].detail


def test_orphan_uv_check_is_clean_when_there_is_none(monkeypatch, tmp_path):
    from nexus.health import _check_orphan_uv_install

    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "empty"))
    results = _check_orphan_uv_install()
    assert results[0].ok is True
