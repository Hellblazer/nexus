# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Side-by-side parity harness over the enumerated release-decision cells
(RDR-201 P2.2, nexus-j9z30.12).

Loads ``tests/scripts/fixtures/release_cells.json`` (built by
``scripts/enumerate_release_cells.py``, RDR-201 P2.1 / nexus-j9z30.11) and,
for EVERY cell, drives the CURRENT decision path -- the real gated function,
via the enumerator's own monkeypatched drivers, never reimplemented here --
and asserts the observed verdict (exit code + message key) matches the
fixture's frozen expected column.

Two roles, one test body
-------------------------

While no table exists (today), this is a CHANGE DETECTOR for the
enumeration itself: ``_new_path`` returns ``NotImplemented`` (nothing to
compare against yet) and ``test_new_path_matches_old_path_cell_by_cell``
skips every case rather than asserting anything.

Once P2.4/P2.5 land ``docs/tables/release-choreography.toml`` and rewire the
two scripts onto ``table.resolve()``, ``_new_path`` starts returning a real
verdict and that same test starts asserting ``old == new`` for every cell
instead of skipping. Nothing else in this file changes for that flip.

Isolation
---------

Inherits ``tests/scripts/conftest.py``'s autouse ``_isolate_wire_contract_
ledger`` fixture -- every test in this directory runs against an EMPTY tmp
ledger unless a cell's own driver monkeypatches ``check_wire_contract_
pairing.parse_ledger`` directly. All of the ledger-bearing drivers exercised
below do exactly that (via ``enumerate_release_cells._ledger_fixture``),
which shadows the autouse patch for the duration of that one driven call --
so the autouse fixture's emptiness is never actually load-bearing for THIS
suite's ledger cells. That is exactly the isolation that let O1 (2026-09-01:
floor/precondition ledger disagreement, live in
``docs/wire-contract-pending.md``, unseen by 24 unrelated logic tests) go
unseen for as long as it did -- so
``test_fixture_carries_nonempty_additive_ledger_cells`` below pins that the
FIXTURE SET itself, independent of test-suite isolation, still exercises a
non-empty, ``[additive]``-entry-bearing ledger state.
"""
from __future__ import annotations

import copy
import json
import pathlib
from typing import Any, Callable

import pytest

import enumerate_release_cells as erc

_FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "release_cells.json"
)
_FIXTURE: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
_FIXTURE_CELLS: list[dict[str, Any]] = _FIXTURE["cells"]
_FIXTURE_CELL_IDS: list[str] = [c["cell_id"] for c in _FIXTURE_CELLS]

#: ``Cell.function`` (as written by ``enumerate_release_cells.build_fixture()``)
#: -> the enumerator's own driver for that function. Every key here is one of
#: the 12 functions ``build_fixture()`` enumerates
#: (``erc._all_chains()`` + ``erc._all_orchestrator_results()``); a 13th
#: function appearing in the fixture with no driver here is a real gap,
#: caught loudly by ``_drive_old_path``'s ``KeyError`` -- never silently
#: skipped.
_OLD_PATH_DRIVERS: dict[str, Callable[[erc.Cell], tuple[int, str]]] = {
    "check_pin_currency": erc.drive_pin_currency,
    "check_source_ancestry": erc.drive_source_ancestry,
    "check_client_lag_ledger": erc.drive_client_lag_ledger,
    "check_wire_contract_ledger": erc.drive_wire_contract_ledger,
    "check_paired_preconditions": erc.drive_paired_preconditions,
    "record_deploy_from_gate_report_leg": erc.drive_tracker_outcome,
    "check_floor_bare": erc.drive_check_floor_bare,
    "check_floor_paired": erc.drive_check_floor_paired_explicit,
    "check_floor_auto_paired": erc.drive_check_floor_auto_paired,
    "main_dispatch": erc.drive_main_dispatch,
    "check_composite": erc.drive_check_composite,
    "precond_main_dispatch": erc.drive_precond_main_dispatch,
}


def _cell_from_fixture(cell_dict: dict[str, Any]) -> erc.Cell:
    return erc.Cell(
        function=cell_dict["function"],
        inputs=cell_dict["inputs"],
        exit_code=cell_dict["exit_code"],
        message_key=cell_dict["message_key"],
        note=cell_dict.get("note", ""),
    )


def _drive_old_path(cell: erc.Cell) -> tuple[int, str]:
    """Drive the CURRENT decision path -- the real gated function -- for one
    cell, by dispatching to the enumerator's own driver for ``cell.function``.
    Never reimplements a driver; a function the fixture names with no entry
    in ``_OLD_PATH_DRIVERS`` fails loudly (``KeyError``), not silently."""
    return _OLD_PATH_DRIVERS[cell.function](cell)


def _new_path(cell: dict[str, Any]) -> tuple[int, str] | Any:
    """P2.4/P2.5 hook: once ``docs/tables/release-choreography.toml`` exists
    and the two gated scripts are rewired onto ``table.resolve()``, this
    drives THAT path for the same cell and returns its verdict. Until then
    there is no table to resolve against, so this returns ``NotImplemented``
    and the parity test below treats every cell as "not yet comparable"
    (an explicit skip) rather than asserting anything."""
    del cell  # unused until P2.4/P2.5 wire a real resolve() call here
    return NotImplemented


def _assert_old_path_matches_fixture(cell_dict: dict[str, Any]) -> None:
    cell = _cell_from_fixture(cell_dict)
    observed = _drive_old_path(cell)
    assert erc.verify_cell(cell, observed), (
        cell_dict["cell_id"], observed, (cell.exit_code, cell.message_key),
    )


# ---------------------------------------------------------------------------
# Non-vacuity: the parametrization itself must be real
# ---------------------------------------------------------------------------

def test_fixture_cell_count_is_nonzero_and_matches_the_fixtures_own_header() -> None:
    """A harness with an empty parametrization passes trivially. Pin both
    that cells exist at all AND that the count in play matches the
    fixture's own declared ``reachable_cell_count`` -- a loader bug that
    silently drops rows (e.g. a bad JSON key) still produces a nonzero but
    WRONG count, which this second assertion catches."""
    assert len(_FIXTURE_CELLS) > 0
    assert len(_FIXTURE_CELLS) == _FIXTURE["header"]["reachable_cell_count"]


#: The 5 cells in the P2.1 fixture (2026-09-02, ``release_cells.json``)
#: whose ledger input is "additive": ``check_client_lag_ledger``,
#: ``check_wire_contract_ledger``, ``main_dispatch`` (``--ledger-only``
#: mode), and ``check_composite`` x2 (vacuous / satisfied hand table). Each
#: is driven through ``enumerate_release_cells._ledger_fixture("additive")``,
#: which builds a genuinely non-empty ``Ledger`` carrying one
#: ``LedgerEntry`` whose note is ``"[additive] safe"`` -- not the empty
#: ledger ``tests/scripts/conftest.py``'s autouse fixture would otherwise
#: substitute.
_EXPECTED_MIN_ADDITIVE_LEDGER_CELLS = 5


def test_fixture_carries_nonempty_additive_ledger_cells() -> None:
    """RDR-201 P2.2 spec: the isolation that hid O1 (the empty-ledger
    autouse fixture in ``tests/scripts/conftest.py``) must not recur here --
    pin that the fixture set itself still contains cells exercising a
    non-empty, ``[additive]``-entry-bearing ledger, independent of what the
    autouse fixture would otherwise substitute."""
    additive_cells = [
        c for c in _FIXTURE_CELLS if c["inputs"].get("ledger") == "additive"
    ]
    assert len(additive_cells) >= _EXPECTED_MIN_ADDITIVE_LEDGER_CELLS, additive_cells
    for cell_dict in additive_cells:
        assert "additive" in cell_dict["message_key"], cell_dict


# ---------------------------------------------------------------------------
# Role 1 (today): old-path-vs-fixture change detector
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cell_dict", _FIXTURE_CELLS, ids=_FIXTURE_CELL_IDS)
def test_old_path_matches_fixture(cell_dict: dict[str, Any]) -> None:
    """Drive the CURRENT decision path for every enumerated cell and assert
    its verdict matches the fixture's frozen expected column. This is the
    parity harness's role while no table exists: a change detector on the
    real gated functions and on the enumeration that describes them."""
    _assert_old_path_matches_fixture(cell_dict)


# ---------------------------------------------------------------------------
# Role 2 (P2.4/P2.5): old-path-vs-new-path parity, once resolve() exists
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cell_dict", _FIXTURE_CELLS, ids=_FIXTURE_CELL_IDS)
def test_new_path_matches_old_path_cell_by_cell(cell_dict: dict[str, Any]) -> None:
    """Once P2.4/P2.5 rewire the scripts onto ``docs/tables/release-
    choreography.toml`` + ``resolve()``, ``_new_path`` starts returning a
    real verdict and this test starts asserting ``old == new`` for every
    cell. Until then ``_new_path`` returns ``NotImplemented`` and every case
    skips -- explicitly, not silently: a skip here is visible in the run
    summary, never reported as a pass."""
    new = _new_path(cell_dict)
    if new is NotImplemented:
        pytest.skip("new path (table resolve()) not wired yet -- RDR-201 P2.4/P2.5")
    cell = _cell_from_fixture(cell_dict)
    old = _drive_old_path(cell)
    assert new == old, (cell_dict["cell_id"], old, new)


# ---------------------------------------------------------------------------
# Harness integrity: a corrupted expected verdict must red the harness
# ---------------------------------------------------------------------------

def test_a_corrupted_expected_verdict_reds_the_harness() -> None:
    """Deliberately corrupt one cell's expected exit code and confirm the
    comparison helper the parametrized test above calls actually fails on
    it -- proof this harness CAN fail, not just a suite that always passes
    because every fixture row happens to already agree with the code."""
    original = _FIXTURE_CELLS[0]
    corrupted = copy.deepcopy(original)
    corrupted["exit_code"] = _a_wrong_exit_code(original["exit_code"])
    with pytest.raises(AssertionError):
        _assert_old_path_matches_fixture(corrupted)


def _a_wrong_exit_code(exit_code: int) -> int:
    """A value guaranteed different from ``exit_code``: the drivers below
    read only ``cell.inputs`` (and, for two documented identical-text
    branches, ``cell.message_key``) to compute the REAL exit code, never
    ``cell.exit_code`` itself -- so the real observed exit code is always
    the un-corrupted one, and any distinct value corrupts the comparison."""
    return exit_code + 1
