# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-58vc9: pins the `--candidate-migration` tuple-space population.

Provenance: the nexus-vpl9c riders critic pass (T2
``nexus/vpl9c-riders-critic-pass-2026-09-13``) found that
``tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh`` populated
content, catalog manifests and taxonomy through the floor engine before the
candidate's Liquibase walk, but never a single ``nexus.tuples`` row -- so
tuples-003 (the body-size cleanup + CHECK constraint) and nexus-8zoyp's
consumed-body nulling would apply against an EMPTY table on every rehearsal,
never a populated, RLS-guarded one.

This is a TEXT lint, not a behavioral one: the actual rehearsal runs inside a
throwaway Docker container with a real native build (tens of minutes), well
outside a unit test's budget. What IS cheaply and mechanically checkable is
that the population and assertion blocks a human reviewer was told exist
actually exist in the script, in the right relative ORDER (population and its
own non-vacuity count-asserts before Stage 4's swap; the post-walk behavioral
asserts after Stage 5's boot) -- so a future edit that quietly deletes one of
these blocks, or reorders population to after the swap (making every count
vacuous), fails a fast test instead of only ever being caught by a human
rereading a script nobody runs locally.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = (
    REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "rehearse_candidate_migration.sh"
)
SKILL = REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md"

#: Markers for each claim-state / body-size / tenant fixture the population
#: stage must seed. Order-independent among themselves -- checked only for
#: PRESENCE here; the ORDER checks below are against the Stage boundaries.
_POPULATION_MARKERS = (
    "Stage 3h",
    "seeded unclaimed mailbox row",
    "seeded claimed-and-left mailbox row",
    "seeded consumed(no-reply) mailbox row",
    "seeded consumed(with-reply) mailbox row",
    "seeded dead-letter-candidate mailbox row",
    "seeded the at-cap (4096-byte body) mailbox row",
    "seeded the over-cap (5000-byte body) mailbox row",
    "seeded a ledger start row",
    "seeded a ledger report row",
    "a 4th claim attempt finds nothing",
    "nx tenant create",
)

#: The non-vacuity count-asserts (nexus-moht0 doctrine): the planned row
#: counts must be asserted to actually exist BEFORE the swap.
_NON_VACUITY_MARKERS = (
    "8 mailbox rows planted",
    "2 ledger rows planted",
    "claim-log row(s) exist pre-walk",
)

#: The post-walk behavioral asserts this leg exists to prove.
_POST_WALK_MARKERS = (
    "chk_tuples_body_size exists and is VALIDATED",
    "RLS ENABLE+FORCE after the walk",
    "the over-cap row is gone after the walk",
    "claim-log row(s) survive with tuple_id NULLed",
    "the at-cap row is intact at exactly 4096 bytes after the walk",
    "row's body is NULL after the walk (nexus-8zoyp contract)",
    "the claimed-and-left row's body is intact after the walk",
    "the dead-lettered row's body is intact after the walk",
    "the candidate can claim and ack a surviving row post-boot",
)

#: Stage boundary markers already present in the script, used to derive
#: ORDER (population/non-vacuity before Stage 4; post-walk asserts after
#: Stage 5's boot). Not something this lint invents -- the script's own
#: `say` banners.
_STAGE_4_BANNER = "Stage 4 — nx daemon service stop"
_STAGE_5_BOOT_BANNER = "nx daemon service start (candidate boot"


def _script_text() -> str:
    return SCRIPT.read_text()


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"rehearsal script moved: {SCRIPT}"


def test_population_markers_present() -> None:
    text = _script_text()
    missing = [m for m in _POPULATION_MARKERS if m not in text]
    assert not missing, (
        f"tuple-space population marker(s) missing from {SCRIPT.name}: {missing} -- "
        "the nexus-58vc9 population stage was removed or reworded without updating this pin"
    )


def test_non_vacuity_markers_present() -> None:
    text = _script_text()
    missing = [m for m in _NON_VACUITY_MARKERS if m not in text]
    assert not missing, (
        f"tuple-space non-vacuity count-assert marker(s) missing from {SCRIPT.name}: {missing} -- "
        "a populate step with no pre-walk count assert can silently seed nothing "
        "and every downstream tuple-space assert would be vacuous (nexus-moht0)"
    )


def test_post_walk_markers_present() -> None:
    text = _script_text()
    missing = [m for m in _POST_WALK_MARKERS if m not in text]
    assert not missing, (
        f"tuple-space post-walk assert marker(s) missing from {SCRIPT.name}: {missing}"
    )


def test_population_and_non_vacuity_precede_stage_4_swap() -> None:
    """Population (and its own non-vacuity counts) must run BEFORE the
    candidate is swapped in at Stage 4 -- seeding AFTER the swap would prove
    nothing about the CANDIDATE's own migration walk over populated data."""
    text = _script_text()
    stage_4_at = text.index(_STAGE_4_BANNER)
    for marker in _POPULATION_MARKERS + _NON_VACUITY_MARKERS:
        at = text.index(marker)
        assert at < stage_4_at, (
            f"{marker!r} appears at or after Stage 4's swap banner "
            f"({at} >= {stage_4_at}) -- tuple-space population/non-vacuity must "
            "run BEFORE the candidate binary is swapped in"
        )


def test_post_walk_asserts_follow_stage_5_boot() -> None:
    """The post-walk behavioral asserts must run AFTER the candidate has
    actually booted (Stage 5) -- asserting migration outcomes before the
    candidate's own Liquibase walk ran would be vacuous by construction."""
    text = _script_text()
    stage_5_at = text.index(_STAGE_5_BOOT_BANNER)
    for marker in _POST_WALK_MARKERS:
        at = text.index(marker)
        assert at > stage_5_at, (
            f"{marker!r} appears at or before Stage 5's candidate-boot banner "
            f"({at} <= {stage_5_at}) -- tuple-space post-walk asserts must run "
            "AFTER the candidate has booted over the populated store"
        )


def test_skill_coverage_paragraph_mentions_tuple_space() -> None:
    """The engine-release skill's own `--candidate-migration` coverage
    paragraph must name the tuple-space addition -- the file-header comment
    in the script and this paragraph are kept in lockstep by convention
    (see both files' own text), so a coverage claim can't drift silently
    between the two the way nexus-1e2eh's class of incident did for other
    release-only procedures."""
    skill_text = SKILL.read_text()
    assert "nexus-58vc9" in skill_text, (
        "SKILL.md's --candidate-migration coverage paragraph does not mention "
        "nexus-58vc9 -- the script header and the skill doc are meant to stay "
        "in lockstep"
    )
    for marker in ("chk_tuples_body_size", "tuple_id=NULL", "nexus-8zoyp"):
        assert marker in skill_text, (
            f"SKILL.md's tuple-space coverage paragraph is missing {marker!r}"
        )
