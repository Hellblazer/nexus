# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 guest walk (nexus-25wlq): the closing hint of ``nx self install``.

On a fresh Windows install the hint said "run `nx upgrade` for migrations"
between two steps that never need it: `nx init` converges the migration ladder
itself (``init._converge_ladder_best_effort``). The hint now says when it applies.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from nexus.commands import self_cmd


def _run() -> str:
    gen = Path("gen-20261008T001446Z")
    with patch.object(self_cmd, "perform_self_install", return_value=gen), \
         patch.object(self_cmd, "prune_uv_cache", return_value="uv cache prune: done"):
        result = CliRunner().invoke(self_cmd.install_cmd, [])
    assert result.exit_code == 0, result.output
    return result.output


def test_the_hint_says_nx_upgrade_applies_to_an_existing_install() -> None:
    out = _run()
    assert "installed gen-20261008T001446Z" in out
    assert "if this upgraded an existing install, run `nx upgrade` for migrations" in out


def test_the_hint_points_a_fresh_install_at_nx_init() -> None:
    assert "a fresh install is set up by `nx init`" in _run()


def test_init_really_converges_the_ladder_so_the_hint_is_true() -> None:
    """The claim the hint and docs/windows-install.md make: init walks the ladder."""
    from nexus.commands import init

    assert callable(init._converge_ladder_best_effort)
    src = (Path(init.__file__)).read_text(encoding="utf-8")
    assert src.count("_converge_ladder_best_effort()") >= 2, (
        "nx init must call the ladder convergence on its service paths"
    )
