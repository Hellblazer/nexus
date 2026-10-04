# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI never sets NX_SUITE_LEASE_UNGUARDED, and the opt-out means exactly "1".

The suite lease fails closed (tests/conftest.py ``_take_suite_lease``): a run that
cannot take it exits 75 instead of running a second substrate-heavy suite beside
the first. ``NX_SUITE_LEASE_UNGUARDED=1`` is the explicit opt-out FOR A HAND RUN.
A workflow that set it would turn the guard off for every CI run on the box that
also hosts the live service, which is the overlap the lease exists to stop, and
"CI never sets it" was prose until this lint (round-3 review S3).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).parent.parent
ENV = "NX_SUITE_LEASE_UNGUARDED"
#: The surfaces a CI run reads its environment from.
_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml", ".github/actions/**/*.yml", ".github/actions/**/*.yaml")
#: Non-vacuity: a sweep that found nothing to check is a failure (nexus-moht0). 23 workflows and 4 actions today.
_MIN_FILES = 15


def _ci_files() -> list[Path]:
    return sorted({p for g in _GLOBS for p in REPO.glob(g)})


def _mentions(text: str) -> list[int]:
    """Line numbers naming the variable at all: an env block, a `export`, a `run:` line, a comment saying to set it."""
    return [i for i, line in enumerate(text.splitlines(), 1) if ENV in line]


def test_no_workflow_or_action_sets_the_suite_lease_opt_out() -> None:
    files = _ci_files()
    assert len(files) >= _MIN_FILES, f"the sweep read only {len(files)} CI files; the globs are broken or the tree moved"
    offenders = {str(p.relative_to(REPO)): lines for p in files if (lines := _mentions(p.read_text()))}
    assert not offenders, (
        f"{ENV} appears in CI config {offenders}: the opt-out is for a hand run only. A CI run that cannot take the "
        "suite lease must exit 75, not run unguarded beside the live service."
    )


def test_the_scan_has_the_power_to_see_each_way_a_workflow_could_set_it() -> None:
    """Positive controls: the shapes a real offender would take are all found by the same predicate."""
    shapes = {
        "job env": "    env:\n      NX_SUITE_LEASE_UNGUARDED: '1'\n",
        "step export": "        run: |\n          export NX_SUITE_LEASE_UNGUARDED=1\n          pytest\n",
        "GITHUB_ENV append": "          echo NX_SUITE_LEASE_UNGUARDED=1 >> \"$GITHUB_ENV\"\n",
        "inline prefix": "        run: NX_SUITE_LEASE_UNGUARDED=1 uv run pytest\n",
    }
    for name, text in shapes.items():
        assert _mentions(text), name
    assert not _mentions("env:\n  NX_SUITE_LEASE_WAIT: 1\n  NX_BUILD_LEASE_ROOT: /x\n")
