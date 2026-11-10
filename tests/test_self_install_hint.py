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


def _run(*, set_up: bool) -> str:
    gen = Path("gen-20261008T001446Z")
    with patch.object(self_cmd, "perform_self_install", return_value=gen), \
         patch.object(self_cmd, "prune_uv_cache", return_value="uv cache prune: done"), \
         patch.object(self_cmd, "_box_is_set_up", return_value=set_up):
        result = CliRunner().invoke(self_cmd.install_cmd, [])
    assert result.exit_code == 0, result.output
    return result.output


def test_the_hint_names_nx_upgrade_for_a_box_already_set_up() -> None:
    out = _run(set_up=True)
    assert "installed gen-20261008T001446Z" in out
    assert "run `nx upgrade` to migrate this install's data" in out
    assert "nx init" not in out


def test_the_hint_points_a_box_never_set_up_at_nx_init() -> None:
    """F23 (nexus-f3yqd.6): the hint used to name both cases and leave the choice to the user."""
    out = _run(set_up=False)
    assert "nothing is set up on this machine yet: run `nx init`" in out
    assert "nx upgrade" not in out


def test_init_really_converges_the_ladder_so_the_hint_is_true() -> None:
    """The claim the hint and docs/windows-install.md make: init walks the ladder."""
    from nexus.commands import init

    assert callable(init._converge_ladder_best_effort)
    import ast

    tree = ast.parse(Path(init.__file__).read_text(encoding="utf-8"))
    # Call nodes only: the def line and a docstring mention are not call sites.
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_converge_ladder_best_effort"
    ]
    # nexus-mjmjl: both service paths reach it through _finish_local_init.
    assert len(calls) >= 1, "nx init must call the ladder convergence"
    finishes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_finish_local_init"
    ]
    assert len(finishes) >= 2, "both nx init service paths must finish through _finish_local_init"
    import inspect

    assert "_converge_ladder_best_effort()" in inspect.getsource(init._finish_local_init)
