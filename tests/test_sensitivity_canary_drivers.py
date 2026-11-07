# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-243 Phase 1 exit (nexus-rfvyy.6.21) and Phase 2 exit (nexus-rfvyy.7.13): the canary's drivers and the
registry rows agree.

``tests/test_sensitivity_canary.py`` is parametrized over the registry, so a row with no driver
fails there. The other direction is silent: a driver keyed by a target the registry no longer names
is never run. This file holds both directions, and a floor on the rows the canary judges, so a
registry that lost its rows (or a loader that returned none) is a failure, never a pass over
nothing. It needs no tenant."""
from __future__ import annotations

from tests._llm_surfaces import load_surfaces
from tests.test_sensitivity_canary import (
    CANARY_BEHAVIOURS,
    NON_SEARCH_EXCLUDE_ROWS,
    PHASE_3_MUTATIONS,
    PHASE_UNDER_TEST,
    SURFACE_DRIVERS,
)

#: Refuse and skip rows at phase 1 (census rows 1-10 and 21). A row leaving the phase or the table lowers
#: this on purpose, in the commit that moves it.
PHASE_1_ROW_FLOOR = 11

#: Exclude rows at or below phase 3 (census rows 12-17 at phase 2, rows 11 and 20 at phase 3).
EXCLUDE_ROW_FLOOR = 8

#: Withhold rows (census rows 23 and 24, phase 3).
WITHHOLD_ROW_FLOOR = 2


def _judged() -> list:
    surfaces = load_surfaces()
    assert surfaces, "the LLM-surface registry did not load: the canary would judge nothing"
    return [s for s in surfaces if s.behaviour in CANARY_BEHAVIOURS and s.phase <= PHASE_UNDER_TEST]


def test_the_canary_judges_every_refuse_skip_exclude_and_withhold_row_of_the_phase() -> None:
    judged = _judged()
    write_side = [s for s in judged if s.behaviour in {"refuse", "skip"}]
    reads = [s for s in judged if s.behaviour == "exclude"]
    withhold = [s for s in judged if s.behaviour == "withhold"]
    assert len(write_side) >= PHASE_1_ROW_FLOOR, f"only {len(write_side)} refuse/skip rows selected: {sorted(s.name for s in write_side)}"
    assert len(reads) >= EXCLUDE_ROW_FLOOR, f"only {len(reads)} exclude rows selected: {sorted(s.name for s in reads)}"
    assert len(withhold) >= WITHHOLD_ROW_FLOOR, f"only {len(withhold)} withhold rows selected: {sorted(s.name for s in withhold)}"


def test_every_phase_3_row_has_a_mutation() -> None:
    """The gate each Phase 3 row stands on is removed in ``test_harness_a_phase_3_gate_removed_turns_its_row_red``;
    a Phase 3 row with no entry in ``PHASE_3_MUTATIONS`` would be judged green with its gate gone, and an entry for
    a target no Phase 3 row names is a mutation that never runs."""
    phase_3 = {s.target for s in _judged() if s.phase == 3}
    assert phase_3, "no Phase 3 row is judged: the phase under test did not reach 3"
    assert sorted(phase_3 - set(PHASE_3_MUTATIONS)) == [], "Phase 3 rows with no mutation"
    assert sorted(set(PHASE_3_MUTATIONS) - phase_3) == [], "mutations keyed by a target no Phase 3 row names"


def test_every_judged_row_has_a_driver_and_every_driver_names_a_judged_row() -> None:
    judged = {s.target for s in _judged()}
    assert sorted(judged - set(SURFACE_DRIVERS)) == [], "rows with no driver"
    assert sorted(set(SURFACE_DRIVERS) - judged) == [], "drivers keyed by a target no judged row names"


def test_a_row_exempt_from_the_pool_assertion_names_a_judged_exclude_row_and_says_why() -> None:
    exclude = {s.target for s in _judged() if s.behaviour == "exclude"}
    assert sorted(set(NON_SEARCH_EXCLUDE_ROWS) - exclude) == [], "exemptions for a target no judged exclude row names"
    assert all(reason.strip() for reason in NON_SEARCH_EXCLUDE_ROWS.values()), "an exemption carries its reason"
