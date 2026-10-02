# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z0o2p.42: pins the `--candidate-migration` seed-client contract.

The leg runs inside a throwaway container with a real native build, so no unit
test can run it. What is cheaply checkable is that the pieces a reviewer was told
exist actually exist, in the order that makes them mean something. Each pin below
is one such claim, and each fails when the line it names is deleted or moved:

* the working-tree wheel is byte-compared against the installed package before
  the candidate is swapped in (the wheel can carry the same version string as the
  release it replaces, so ``nx --version`` cannot tell the two apart);
* the pending upgrade rungs are read BEFORE ``nx upgrade`` (an upgrade repairs
  rung state, so a read after it can no longer see a rung the candidate boot
  regressed), and the post-upgrade assertions follow it;
* the shared-chash note is seeded and proved non-vacuous before the swap;
* the floor engine is re-read after population, before the swap;
* the seed derivation carries both bounds and its no-release refusal names
  ``git fetch --tags``;
* the engine-release skill's coverage statement, which the script header says is
  kept in lockstep, carries the seed-generation paragraph and the fifth
  "cannot catch" class.

The behavior of the derivation itself is covered by
``tests/e2e/migration-rehearsal/run_sh_guard_test.sh``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
REHEARSAL_DIR = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal"
SCRIPT = REHEARSAL_DIR / "rehearse_candidate_migration.sh"
RUN_SH = REHEARSAL_DIR / "run.sh"
DOCKERFILE = REHEARSAL_DIR / "Dockerfile.candidate-migration"
SKILL = REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md"


def _at(text: str, marker: str) -> int:
    assert text.count(marker) >= 1, f"marker missing from the script: {marker!r}"
    return text.index(marker)


def test_wheel_byte_compare_runs_between_the_client_upgrade_and_the_swap() -> None:
    text = SCRIPT.read_text()
    upgrade = _at(text, "Stage 4 — upgrade the client: uv tool install --reinstall")
    compare = _at(text, "different=0 missing=0")
    non_vacuous = _at(text, '"$WHEEL_CMP" != *"same=0 "*')
    swap = _at(text, "Stage 4 — hand-swap the locally-built CANDIDATE binary")
    assert upgrade < compare < swap and upgrade < non_vacuous < swap, (
        "the byte-compare of the wheel against the installed package (with its same>0 "
        "non-vacuity clause) must sit between the client upgrade and the candidate swap"
    )
    assert "exit 1" in text[compare : compare + 400] or "ABORT" in text[compare : compare + 400], (
        "a failed byte-compare must abort the leg, not just report"
    )


def test_pending_rungs_are_read_before_nx_upgrade_and_asserted_after_it_runs() -> None:
    text = SCRIPT.read_text()
    tree_rungs = _at(text, 'TREE_RUNGS="$(_ladder_rungs)"')
    pending_read = _at(text, "pending upgrade rungs before nx upgrade")
    upgrade_call = _at(text, 'UPG_OUT="$(nx upgrade')
    record = _at(text, "rdr192-manifest-backfill completion record present")
    census = _at(text, "census scope_chunk_total=$CENSUS_SCOPE equals the SQL chunk count")
    doctor = _at(text, "Assert — nx doctor: clean, no pending rungs")
    assert tree_rungs < pending_read < upgrade_call < record < census < doctor, (
        "order must be: tree rung set, pending read, nx upgrade, backfill record, census scope, "
        "post-upgrade doctor. A pending read after nx upgrade cannot see a rung the candidate boot "
        "regressed, because the upgrade repairs it."
    )
    assert "read PENDING after the candidate boot, before nx upgrade" in text


def test_shared_chash_is_seeded_and_proved_before_the_swap() -> None:
    text = SCRIPT.read_text()
    seeded = _at(text, "shared-chash shape seeded: 1 chunk row named by 2 manifest rows")
    vacuous = _at(text, "the census scope assert below would be vacuous")
    swap = _at(text, "Stage 4 — nx daemon service stop")
    assert seeded < swap and vacuous < swap


def test_floor_engine_is_re_read_after_population_before_the_swap() -> None:
    text = SCRIPT.read_text()
    close = _at(text, "Stage 3 close — the populated engine is still the floor")
    first_stage_3 = _at(text, "Stage 3a — seed")
    swap = _at(text, "Stage 4 — nx daemon service stop")
    assert first_stage_3 < close < swap


def test_seed_derivation_bounds_and_refusal_text() -> None:
    text = RUN_SH.read_text()
    for marker in (
        "NEWER than the floor",  # the pin bound
        "newer than this tree's own version",  # the version bound
        "BELOW the floor",  # the loud notice for a pin under the floor
        "git fetch --tags",  # the no-release refusal names the likely cause
        "cannot derive the --candidate-migration SEED release",
    ):
        assert marker in text, f"run.sh seed derivation lost {marker!r}"


def test_dockerfile_header_does_not_claim_the_leg_never_swaps_packages() -> None:
    assert "never swaps client" not in DOCKERFILE.read_text(), (
        "Stage 4 installs the working-tree wheel over the seed release; the header must not say otherwise"
    )


def test_skill_coverage_statement_carries_the_seed_generation_paragraph() -> None:
    """The script header and the skill's coverage statement are kept in lockstep by
    convention; this pins the markers that convention is about, in both files."""
    skill = SKILL.read_text()
    header = SCRIPT.read_text()
    for marker in ("nexus-z0o2p.42", "five classes", "nexus-wbfpw.60", "git fetch --tags"):
        assert marker in skill, f"SKILL.md's --candidate-migration coverage is missing {marker!r}"
    for marker in ("nexus-z0o2p.42", "five classes", "nexus-wbfpw.60"):
        assert marker in header, f"the script header is missing {marker!r}"
    assert "four classes" not in skill and "four classes" not in header
