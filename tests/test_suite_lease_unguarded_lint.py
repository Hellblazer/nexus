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


#: The ONE line CI may carry: ci.yml's lease step REFUSES a job whose environment already has the opt-out set
#: (a host-side runner .env could put it there, which no lint of the repo can see; round-4 review L5). A test for
#: emptiness cannot set the variable, so it is the only shape allowed, matched whole, in ci.yml only.
GUARD_LINE = 'if [ -n "${NX_SUITE_LEASE_UNGUARDED:-}" ]; then'
GUARD_FILE = ".github/workflows/ci.yml"


def _mentions(text: str, *, allow_guard: bool = False) -> list[int]:
    """Line numbers naming the variable at all: an env block, a `export`, a `run:` line, a comment saying to set it.

    With *allow_guard*, the single sanctioned refusal line (exactly ``GUARD_LINE`` once stripped) is not a mention.
    """
    return [
        i for i, line in enumerate(text.splitlines(), 1)
        if ENV in line and not (allow_guard and line.strip() == GUARD_LINE)
    ]


def test_no_workflow_or_action_sets_the_suite_lease_opt_out() -> None:
    files = _ci_files()
    assert len(files) >= _MIN_FILES, f"the sweep read only {len(files)} CI files; the globs are broken or the tree moved"
    offenders = {
        rel: lines for p in files
        if (lines := _mentions(p.read_text(), allow_guard=(rel := str(p.relative_to(REPO))) == GUARD_FILE))
    }
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


def test_ci_yml_refuses_a_job_whose_environment_already_has_the_opt_out() -> None:
    """The guard line exists exactly once, in the lease step, before the lease root is used, and exits 1."""
    text = (REPO / GUARD_FILE).read_text()
    assert text.count(GUARD_LINE) == 1, "the guard must be present once; without it a host-side .env disarms the lease silently"
    start = text.index(GUARD_LINE)
    guard_block = text[start:text.index("\n          fi\n", start)]
    assert "exit 1" in guard_block, "the guard must fail the job, not warn"
    after = text[start:]
    assert after.index('root="$QWEN_SUITE_LEASE_ROOT"') < after.index("NX_SUITE_LEASE_WAIT=1"), "and it precedes the lease export"
    last_step_header = text[:start][text[:start].rindex("- name:"):].splitlines()[0]
    assert "Shared suite lease directory" in last_step_header, last_step_header


def test_the_guard_exemption_is_exact_and_confined_to_ci_yml() -> None:
    """Positive controls for the exemption: it must not become a way to set the variable."""
    assert not _mentions(f"          {GUARD_LINE}\n", allow_guard=True)
    assert _mentions(f"          {GUARD_LINE}\n"), "off by default: any other file gets no exemption"
    for offender in (
        '          export NX_SUITE_LEASE_UNGUARDED=1\n',
        '          if [ -n "${NX_SUITE_LEASE_UNGUARDED:-}" ]; then export NX_SUITE_LEASE_UNGUARDED=1; fi\n',
        '          echo NX_SUITE_LEASE_UNGUARDED=1 >> "$GITHUB_ENV"\n',
        '          # set NX_SUITE_LEASE_UNGUARDED to skip the lease\n',
    ):
        assert _mentions(offender, allow_guard=True), offender
