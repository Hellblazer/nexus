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

While no table exists (today), Role 1 (``test_old_path_matches_fixture``)
is a CHANGE DETECTOR for the enumeration itself, with two independent
failure modes: (a) a real gated script's branch logic drifting from what
the fixture recorded (each cell's driver re-executes the real function),
and (b) the checked-in fixture going STALE relative to
``enumerate_release_cells.py``'s own modeling -- e.g. someone edits a cell
table in the enumerator but forgets to regenerate the JSON
(``test_committed_fixture_is_not_stale_relative_to_a_fresh_enumeration``
below, which compares ``erc.build_fixture()`` -- a pure, deterministic
function -- against the committed file directly).

``_new_path`` returns ``NotImplemented`` (nothing to compare against yet)
and ``test_new_path_matches_old_path_cell_by_cell`` skips every case rather
than asserting anything.

Once P2.4/P2.5 land ``docs/tables/release-choreography.toml`` and rewire the
two scripts onto ``table.resolve()``, ``_new_path`` starts returning a real
verdict. At that point Role 2 is a claim per **(cell, event)** pair, not per
cell alone: the OLD path -- a bare call into a gated function -- has no way
to branch on the EVENT dimension (pre-tag / tag-push / deploy /
post-deploy-verify) at all, while the new table's ``event`` column means the
SAME cell reached at two different events is not guaranteed to resolve to
the same new-path verdict. ``_new_path`` therefore already takes
``(cell, event)`` today, and the Role-2 test is already parametrized over
every reachable ``(cell, event)`` pair (still skipping on ``NotImplemented``)
so P2.4/P2.5 only has to change what ``_new_path`` returns, never this
file's parametrization shape. The (cell -> reachable events) mapping used
here is a P2.2-time CONSERVATIVE over-approximation -- see
``_cell_reachable_events`` below -- deliberately coarser than the precise
per-cell join P2.3's own table authoring will make possible.

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
import random
from typing import Any, Callable

import pytest

import enumerate_release_cells as erc

_FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "release_cells.json"
)
_FIXTURE: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
_FIXTURE_CELLS: list[dict[str, Any]] = _FIXTURE["cells"]
_FIXTURE_CELL_IDS: list[str] = [c["cell_id"] for c in _FIXTURE_CELLS]

#: The choreography table P2.4/P2.5 will author. Does not exist yet.
_CHOREOGRAPHY_TABLE_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "docs" / "tables"
    / "release-choreography.toml"
)

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

#: Which of the two gated scripts each cell-producing function belongs to.
#: ``erc.event_mode_matrix()`` is keyed by (script, mode) -- there is no
#: per-cell-producing-function event mapping to read directly, so this is
#: a manually-maintained, exhaustiveness-checked partition (see
#: ``test_function_script_map_covers_every_fixture_function`` below).
_FLOOR_SCRIPT_FUNCTIONS = frozenset({
    "check_pin_currency", "check_source_ancestry", "check_client_lag_ledger",
    "check_paired_preconditions", "record_deploy_from_gate_report_leg",
    "check_floor_bare", "check_floor_paired", "check_floor_auto_paired",
    "main_dispatch",
})
_PRECOND_SCRIPT_FUNCTIONS = frozenset({
    "check_wire_contract_ledger", "check_composite", "precond_main_dispatch",
})
_FUNCTION_SCRIPT: dict[str, str] = {
    **{f: "check_engine_release_floor" for f in _FLOOR_SCRIPT_FUNCTIONS},
    **{f: "check_client_release_precondition" for f in _PRECOND_SCRIPT_FUNCTIONS},
}


def _script_reachable_events() -> dict[str, tuple[str, ...]]:
    """The union of events reachable by ANY mode of each script, per
    ``erc.event_mode_matrix()`` (the static mode -> event citations table)."""
    by_script: dict[str, set[str]] = {}
    for row in erc.event_mode_matrix():
        if row["reachable"]:
            by_script.setdefault(row["script"], set()).add(row["event"])
    return {script: tuple(sorted(events)) for script, events in by_script.items()}


_SCRIPT_REACHABLE_EVENTS = _script_reachable_events()


def _cell_reachable_events(cell_dict: dict[str, Any]) -> tuple[str, ...]:
    """P2.2 conservative mapping: a cell's reachable events = the UNION of
    reachable events across every MODE its script exposes -- coarser than
    the exact per-mode/per-cell attribution P2.3's table-authoring join
    will make possible (e.g. ``check_pin_currency`` is reached by three of
    the floor script's four modes, at different event sets each), but
    conservative in the SAFE direction: it can only add MORE (cell, event)
    parametrized cases than the eventual precise join, never fewer, so
    nothing is silently excluded from Role 2's future coverage."""
    script = _FUNCTION_SCRIPT[cell_dict["function"]]
    return _SCRIPT_REACHABLE_EVENTS[script]


_CELL_EVENT_PAIRS: list[tuple[dict[str, Any], str]] = [
    (cell_dict, event)
    for cell_dict in _FIXTURE_CELLS
    for event in _cell_reachable_events(cell_dict)
]
_CELL_EVENT_IDS: list[str] = [
    f"{cell_dict['cell_id']}@{event}" for cell_dict, event in _CELL_EVENT_PAIRS
]

_CORRUPTION_SAMPLE_SEED = 20260902
_CORRUPTION_SAMPLE_SIZE = 3


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


def _new_path(cell: dict[str, Any], event: str) -> tuple[int, str] | Any:
    """P2.4/P2.5 hook: once ``docs/tables/release-choreography.toml`` exists
    and the two gated scripts are rewired onto ``table.resolve()``, this
    drives THAT path for the same ``(cell, event)`` pair and returns its
    verdict. ``event`` is part of the signature from the start (RDR-201
    P2.2 critique, T2 nexus/critique-nexus-j9z30-12-2026-09-01): the OLD
    path cannot branch on event at all, but the new table's ``event``
    column means the same cell can resolve differently across events, so
    Role 2 parity has to be checked per (cell, event), not per cell alone.
    Until the table exists there is nothing to resolve against, so this
    returns ``NotImplemented`` and the parity test below treats every
    (cell, event) pair as "not yet comparable" (an explicit skip) rather
    than asserting anything."""
    del cell, event  # unused until P2.4/P2.5 wire a real resolve() call here
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


def test_function_script_map_covers_every_fixture_function() -> None:
    """``_FUNCTION_SCRIPT`` is hand-maintained (there is no machine-readable
    function -> script mapping to read from the enumerator). If a 13th
    cell-producing function ever appears in the fixture with no entry here,
    ``_cell_reachable_events`` would ``KeyError`` deep inside test
    collection with a confusing traceback -- this test fails loudly and
    specifically instead, at the actual point of drift."""
    fixture_functions = {c["function"] for c in _FIXTURE_CELLS}
    assert fixture_functions == set(_FUNCTION_SCRIPT), (
        fixture_functions - set(_FUNCTION_SCRIPT),
        set(_FUNCTION_SCRIPT) - fixture_functions,
    )


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


def test_committed_fixture_is_not_stale_relative_to_a_fresh_enumeration() -> None:
    """Role 1's OTHER independent value (RDR-201 P2.2 critique, T2
    nexus/critique-nexus-j9z30-12-2026-09-01), distinct from re-deriving
    each cell's verdict against the real gated scripts: detecting a STALE
    checked-in fixture -- one where ``enumerate_release_cells.py``'s own
    modeling (which cells exist, their inputs, the dimensions/exclusions
    header) has drifted from ``tests/scripts/fixtures/release_cells.json``
    on disk, e.g. because a change to the enumerator's cell tables was
    never followed by `uv run python scripts/enumerate_release_cells.py`.
    ``erc.build_fixture()`` is a pure, deterministic function -- no
    ``datetime.now()``, no filesystem writes -- so regenerating it
    in-memory and comparing against the committed JSON exercises exactly
    that drift, independent of whether any individual cell's real-function
    verdict has changed."""
    assert erc.build_fixture() == _FIXTURE


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
# Role 2 (P2.4/P2.5): old-path-vs-new-path parity, per (cell, event) pair
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cell_dict,event", _CELL_EVENT_PAIRS, ids=_CELL_EVENT_IDS)
def test_new_path_matches_old_path_cell_by_cell(
    cell_dict: dict[str, Any], event: str,
) -> None:
    """Once P2.4/P2.5 rewire the scripts onto ``docs/tables/release-
    choreography.toml`` + ``resolve()``, ``_new_path`` starts returning a
    real verdict and this test starts asserting ``old == new`` for every
    reachable (cell, event) pair -- see the module docstring for why event
    is part of the comparison, not folded away. Until then ``_new_path``
    returns ``NotImplemented`` and every case skips -- explicitly, not
    silently: a skip here is visible in the run summary, never reported as
    a pass."""
    new = _new_path(cell_dict, event)
    if new is NotImplemented:
        pytest.skip("new path (table resolve()) not wired yet -- RDR-201 P2.4/P2.5")
    cell = _cell_from_fixture(cell_dict)
    old = _drive_old_path(cell)
    assert new == old, (cell_dict["cell_id"], event, old, new)


# ---------------------------------------------------------------------------
# Wiring-completeness canary: a landed table with an unflipped hook must be loud
# ---------------------------------------------------------------------------

def test_wiring_completeness_canary_for_new_path() -> None:
    """code-review finding (T2 nexus/code-review-nexus-j9z30-12-2026-09-01
    §5): if ``docs/tables/release-choreography.toml`` exists on disk,
    ``_new_path`` must not still return ``NotImplemented`` -- that would
    mean P2.4/P2.5 landed the table without anyone flipping this file's
    hook, and the entire Role-2 suite would go on silently skipping
    forever. Today the file does not exist, so this canary passes
    correctly (there is nothing to be loud about yet); it becomes a real,
    failing tripwire the moment the table is authored, until ``_new_path``
    is actually rewired."""
    if not _CHOREOGRAPHY_TABLE_PATH.is_file():
        pytest.skip(
            "docs/tables/release-choreography.toml does not exist yet -- "
            "RDR-201 P2.4/P2.5"
        )
    sample_cell, sample_event = _CELL_EVENT_PAIRS[0]
    result = _new_path(sample_cell, sample_event)
    assert result is not NotImplemented, (
        "docs/tables/release-choreography.toml exists but _new_path() still "
        "returns NotImplemented -- RDR-201 P2.4/P2.5 landed the table "
        "without wiring this file's _new_path() hook to table.resolve()."
    )


# ---------------------------------------------------------------------------
# Harness integrity: a corrupted expected verdict must red the harness
# ---------------------------------------------------------------------------

def _corruption_sample() -> list[dict[str, Any]]:
    """A seeded random sample of 3 distinct cells, spanning driver
    categories rather than always exercising ``check_pin_currency`` alone
    (code-review Suggestion, T2 nexus/code-review-nexus-j9z30-12-2026-09-01
    §3) -- seeded so the sample (and therefore the parametrized test ids)
    is stable across runs."""
    rng = random.Random(_CORRUPTION_SAMPLE_SEED)
    return rng.sample(_FIXTURE_CELLS, _CORRUPTION_SAMPLE_SIZE)


_CORRUPTION_SAMPLE = _corruption_sample()
_CORRUPTION_SAMPLE_IDS = [c["cell_id"] for c in _CORRUPTION_SAMPLE]


@pytest.mark.parametrize("original", _CORRUPTION_SAMPLE, ids=_CORRUPTION_SAMPLE_IDS)
def test_a_corrupted_expected_verdict_reds_the_harness(
    original: dict[str, Any],
) -> None:
    """Deliberately corrupt one cell's expected exit code and confirm the
    comparison helper the parametrized Role-1 test above calls actually
    fails on it -- proof this harness CAN fail, not just a suite that
    always passes because every fixture row happens to already agree with
    the code. Parametrized over a seeded sample of 3 cells so catchability
    is demonstrated across more than one function's driver."""
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
