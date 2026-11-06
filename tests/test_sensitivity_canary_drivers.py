# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-243 Phase 1 exit (nexus-rfvyy.6.21): the canary's drivers and the registry rows agree.

``tests/test_sensitivity_canary.py`` is parametrized over the registry, so a row with no driver
fails there. The other direction is silent: a driver keyed by a target the registry no longer names
is never run. This file holds both directions, and a floor on the rows the canary judges, so a
registry that lost its refuse and skip rows (or a loader that returned none) is a failure, never a
pass over nothing. It needs no tenant."""
from __future__ import annotations

from tests._llm_surfaces import load_surfaces
from tests.test_sensitivity_canary import CANARY_BEHAVIOURS, PHASE_UNDER_TEST, SURFACE_DRIVERS

#: Refuse and skip rows at phase 1 when the canary exited Phase 1 (census rows 1-10 and 21). A row
#: leaving the phase or the table lowers this on purpose, in the commit that moves it.
PHASE_1_ROW_FLOOR = 11


def _judged() -> list:
    surfaces = load_surfaces()
    assert surfaces, "the LLM-surface registry did not load: the canary would judge nothing"
    return [s for s in surfaces if s.behaviour in CANARY_BEHAVIOURS and s.phase <= PHASE_UNDER_TEST]


def test_the_canary_judges_every_refuse_and_skip_row_of_the_phase() -> None:
    judged = _judged()
    assert len(judged) >= PHASE_1_ROW_FLOOR, f"only {len(judged)} rows selected: {sorted(s.name for s in judged)}"


def test_every_judged_row_has_a_driver_and_every_driver_names_a_judged_row() -> None:
    judged = {s.target for s in _judged()}
    assert sorted(judged - set(SURFACE_DRIVERS)) == [], "rows with no driver"
    assert sorted(set(SURFACE_DRIVERS) - judged) == [], "drivers keyed by a target no judged row names"
