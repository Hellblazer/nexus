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
every reachable ``(cell, event)`` pair. RDR-201 P2.3 (nexus-j9z30.13) has
now landed the table and wired ``_new_path`` for real, so Role 2 no longer
skips -- every pair below asserts ``old == new``. The (cell -> reachable
events) mapping used here is STILL the P2.2-time CONSERVATIVE
over-approximation -- see ``_cell_reachable_events`` below -- P2.3 did NOT
deliver the exact per-mode/per-cell join this docstring used to promise:
what it actually delivered is a table whose verdicts are event/mode-
INVARIANT (every row's guard omits ``event``/``mode`` entirely, verified
against commit 79fff05a9, the actual 7.1.0/v0.1.62 fix). Whether the
choreography SHOULD instead be event-sensitive -- which would make the
per-mode/per-cell precision this mapping was coarser than actually
matter -- is an OPEN, Sam-gated question owned by nexus-j9z30.26, not
settled by this reduction. The conservative mapping stays exactly because
that question is open: it is the safe direction (more (cell, event)
cases than a precise join would produce, never fewer), so nothing is
silently excluded from a future event-sensitive table's coverage either.

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
import dataclasses
import functools
import json
import pathlib
import random
from typing import Any, Callable

import pytest

import check_client_release_precondition as _precond
import check_engine_release_floor as _floor
import enumerate_release_cells as erc
import release_messages
from nexus.tables.load import Table, load_table
from nexus.tables.resolve import resolve

_FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "release_cells.json"
)
_FIXTURE: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
_FIXTURE_CELLS: list[dict[str, Any]] = _FIXTURE["cells"]
_FIXTURE_CELL_IDS: list[str] = [c["cell_id"] for c in _FIXTURE_CELLS]

#: The choreography table (RDR-201 P2.3, nexus-j9z30.13).
_CHOREOGRAPHY_TABLE_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "docs" / "tables"
    / "release-choreography.toml"
)

#: The sentinel enumerate_release_cells.py's hand-enumerated orchestrator
#: cells use for "this axis is not examined on this branch" (e.g.
#: check_floor_bare's pin_currency=blocks cell carries probe="n/a"). The
#: choreography table drops it from every affected dimension's domain
#: entirely (see the table's own file header) -- a resolve() assignment
#: must never carry it, or _validate would refuse the whole call as
#: out-of-domain even though the winning row's guard never examines that
#: key at all.
_NOT_APPLICABLE = "n/a"

#: A placeholder domain value for a declared dimension no row in the
#: cell's own function-group examines (RDR-201 P2.3 design: "event" and
#: "mode" are declared table-wide per the bead's spec but referenced by
#: no row's guard in TODAY's table -- see the table's own file header).
#: Whether the choreography SHOULD be event-sensitive is an open,
#: Sam-gated question (nexus-j9z30.26); this placeholder is a mechanical
#: consequence of today's table shape, not a claim that the question is
#: settled. Any in-domain value resolves identically against today's
#: table; the first declared domain member is used deterministically.
_UNUSED_DIM_PLACEHOLDER_INDEX = 0


@functools.lru_cache(maxsize=1)
def _load_choreography_table() -> Table:
    """Loaded once, not per (cell, event) case -- the table is immutable
    for the life of the test process and Role 2 resolves it hundreds of
    times."""
    return load_table(_CHOREOGRAPHY_TABLE_PATH)

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
    reachable events across every MODE its script exposes (e.g.
    ``check_pin_currency`` is reached by three of the floor script's four
    modes, at different event sets each) -- coarser than a precise
    per-mode/per-cell attribution would be. RDR-201 P2.3 (nexus-j9z30.13)
    did NOT narrow this to that precise join: the table it authored makes
    every cell's verdict event/mode-invariant instead (no row's guard
    examines ``event``/``mode`` at all), so there is currently no per-mode
    event set to join against here. Whether a future, event-sensitive
    table should replace this coarse mapping with a precise one is part of
    the open question nexus-j9z30.26 owns. This mapping stays conservative
    in the SAFE direction regardless of how that question resolves: it can
    only add MORE (cell, event) parametrized cases than a precise join
    would, never fewer, so nothing is silently excluded from Role 2's
    coverage either way."""
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


def _precond_main_dispatch_wiring_case(inputs: dict[str, str]) -> str:
    """``precond_main_dispatch``'s table guard uses ONE synthetic
    ``wiring_case`` dimension (2 values) rather than the raw
    ``engine_tag``/``ack`` input keys, deliberately: the fixture's own 2
    cells are two REPRESENTATIVE wiring scenarios, not a real 2x2
    cross-product (the other two combinations are never enumerated at
    all -- see the table's own comment on this group), so declaring
    ``engine_tag``/``ack`` as two independent guard dimensions would make
    the checker demand coverage for combinations the fixture never
    claims to have verified. This derives the synthetic value from the
    cell's real inputs; an input pair outside the fixture's own two
    combinations is a fixture change this transform has not kept up
    with, so it fails loudly rather than guessing."""
    if inputs == {"engine_tag": "explicit", "ack": "given"}:
        return "engine_tag_explicit_ack_given"
    if inputs == {"engine_tag": "default", "ack": "absent"}:
        return "engine_tag_default_ack_absent"
    raise AssertionError(
        f"precond_main_dispatch: unrecognised inputs {inputs!r} -- the "
        "table's wiring_case dimension only covers the fixture's own two "
        "representative combinations"
    )


#: Function -> transform from the fixture's raw ``cell.inputs`` to the
#: table's own guard-dimension assignment (keyed WITHOUT the function
#: prefix; ``_assignment_for`` below applies it). Only functions whose
#: table dimensions are NOT a direct 1:1 rename of the fixture's own
#: input keys need an entry here -- seven of the twelve table groups
#: reuse the fixture's input key names verbatim (e.g.
#: ``check_pin_currency.newest`` <- ``inputs["newest"]``) and need no
#: transform at all.
_SYNTHETIC_GUARD_TRANSFORMS: dict[str, Callable[[dict[str, str]], dict[str, str]]] = {
    "precond_main_dispatch": lambda inputs: {
        "wiring_case": _precond_main_dispatch_wiring_case(inputs)
    },
}


def _assignment_for(table: Table, cell_dict: dict[str, Any], event: str) -> dict[str, str]:
    """Build a full ``resolve()`` assignment for ``cell_dict`` at ``event``.

    ``resolve()`` requires a value for EVERY declared table dimension
    (``nexus.tables.resolve._validate``), not just the ones ``cell_dict``'s
    own function-group guards on -- so this binds, in order: the ``function``
    match key; ``cell_dict["inputs"]`` transcribed onto the table's
    function-prefixed dimension names (``"<function>.<key>"``, dropping any
    ``"n/a"`` sentinel -- see ``_NOT_APPLICABLE`` above -- and routed through
    ``_SYNTHETIC_GUARD_TRANSFORMS`` for the one function whose table
    dimension is not a direct rename of its fixture input keys); ``event``
    itself; and, for every OTHER declared dimension the cell's own inputs
    never touch (including the intentionally-guard-unreferenced
    ``event``/``mode`` when not already set, and every OTHER function's own
    dimensions), a deterministic placeholder from that dimension's own
    domain -- harmless by construction, since no row outside the cell's own
    match group ever examines them (RDR-201 P2.3 design, see the table's
    file header)."""
    function = cell_dict["function"]
    assignment: dict[str, str] = {"function": function, "event": event}
    transform = _SYNTHETIC_GUARD_TRANSFORMS.get(function)
    raw_inputs = transform(cell_dict["inputs"]) if transform else cell_dict["inputs"]
    for key, value in raw_inputs.items():
        if value == _NOT_APPLICABLE:
            continue
        assignment[f"{function}.{key}"] = value
    for name, dim in table.dimensions.items():
        if name not in assignment:
            assignment[name] = dim.domain[_UNUSED_DIM_PLACEHOLDER_INDEX]
    return assignment


def _new_path(
    cell: dict[str, Any], event: str, table: Table | None = None,
) -> tuple[int, str] | Any:
    """RDR-201 P2.4 (nexus-j9z30.14): for the FLOOR script's own nine
    cell-producing functions, drive the REAL gated function with
    ``check_engine_release_floor.DECISION_PATH`` flipped to ``"table"`` --
    this exercises the ACTUAL production wiring (``_emit_choreography()``
    inside ``check_engine_release_floor.py``), not just the table in
    isolation. Reuses ``_OLD_PATH_DRIVERS[function]`` unchanged: the same
    driver monkeypatches the sensors and invokes the real gated function
    either way, so which decision path fires depends only on the flag.

    The precondition script's own three cell-producing functions
    (``check_wire_contract_ledger`` / ``check_composite`` /
    ``precond_main_dispatch``) are UNCHANGED by this bead
    (nexus-j9z30.15 rewires ``check_client_release_precondition.py`` next)
    -- for those, this keeps resolving ``table`` directly (RDR-201 P2.3's
    original behavior), a preview of what .15 will wire for real.

    ``table`` (the corrupted-copy override ``test_a_mutated_table_row_reds
    _role_2`` below needs) is honored only on the direct-resolve() branch --
    the real gated function always reads the REAL file via its own
    module-level cache, so a floor-script cell cannot be driven against an
    in-memory-only mutation; that canary therefore samples a
    precondition-script cell instead (see its own docstring).

    A resolve() REFUSAL (no-match / ambiguous-match / unknown-value) on a
    cell the fixture itself enumerated as reachable is a table-authoring
    defect, not a legitimate outcome -- raised loudly here rather than
    folded into the tuple return, so it fails the specific (cell, event)
    test case with a clear cause instead of a confusing tuple-shape
    mismatch against ``_drive_old_path``'s output."""
    function = cell["function"]
    if function in _FLOOR_SCRIPT_FUNCTIONS:
        driven_cell = _cell_from_fixture(cell)
        original_path = _floor.DECISION_PATH
        _floor.DECISION_PATH = "table"
        try:
            return _OLD_PATH_DRIVERS[function](driven_cell)
        finally:
            _floor.DECISION_PATH = original_path

    if table is None:
        table = _load_choreography_table()
    assignment = _assignment_for(table, cell, event)
    resolution = resolve(table, assignment)
    if resolution.refusal is not None:
        raise AssertionError(
            f"{cell['cell_id']}@{event}: table refused a cell the fixture "
            f"enumerated as reachable -- {resolution.refusal} {dict(resolution.detail)} "
            f"(assignment={assignment})"
        )
    outcome = resolution.row.outcome
    assert isinstance(outcome, dict), (cell["cell_id"], resolution.row.id, outcome)
    return int(outcome["exit_code"]), outcome["message_key"]


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
    """RDR-201 P2.3 (nexus-j9z30.13): ``_new_path`` now resolves
    ``docs/tables/release-choreography.toml`` for real, so this asserts
    ``old == new`` for every reachable (cell, event) pair -- see the
    module docstring for why event is part of the comparison, not folded
    away. Today's table happens to make every row's verdict event/mode-
    invariant, but whether the CHOREOGRAPHY *should* be event-sensitive is
    an OPEN, Sam-gated question (nexus-j9z30.26, critique T2
    nexus/critique-nexus-j9z30-13-2026-09-01 [24060]) -- the same posture
    as the O2 order-asymmetry (nexus-j9z30.17). A mismatch here must stay
    LOUD and UNDECIDED: it is real signal (either a mistranscribed guard,
    or evidence bearing on nexus-j9z30.26's question), never something
    this test resolves for itself by declaring one interpretation correct."""
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
    mean the table landed without anyone flipping this file's hook, and
    the entire Role-2 suite would go on silently skipping forever. The
    table now exists (RDR-201 P2.3, nexus-j9z30.13) and ``_new_path`` is
    wired, so this is a real, live tripwire from here on -- the
    ``skip`` branch below stays only for a checkout that predates this
    bead."""
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


# ---------------------------------------------------------------------------
# Message catalog <-> table row-id parity (RDR-201 P2.3, nexus-j9z30.13)
# ---------------------------------------------------------------------------
#
# scripts/release_messages.py's RELEASE_MESSAGES is keyed by the SAME row
# id the choreography table uses (f"{function}::{message_key}",
# erc.cell_id's own format) -- this is the "a test asserts every table row
# id has a catalog entry and vice versa" requirement from the bead spec.
# Neither direction is allowed to drift silently: an orphan table row
# (no catalog entry) would ship a release-gate decision with no operator-
# facing prose behind it; an orphan catalog entry (no table row) is dead
# weight nobody's resolve() call can ever reach.


def test_message_catalog_matches_table_row_ids_exactly() -> None:
    table = _load_choreography_table()
    table_ids = {row.id for row in table.rows}
    catalog_ids = set(release_messages.RELEASE_MESSAGES)
    assert table_ids, "the choreography table has zero rows -- nothing to check"
    assert table_ids == catalog_ids, (
        "table rows with no catalog entry: "
        f"{sorted(table_ids - catalog_ids)}; "
        "catalog entries with no table row: "
        f"{sorted(catalog_ids - table_ids)}"
    )


def test_message_catalog_entries_are_nonempty_strings() -> None:
    """Non-vacuity: a catalog whose values are all ``""`` would satisfy the
    id-parity test above while carrying no actual message text."""
    for row_id, text in release_messages.RELEASE_MESSAGES.items():
        assert isinstance(text, str) and text.strip(), row_id


#: Row id -> the real, IMPORTED module-level constant that row's catalog
#: entry must contain VERBATIM (code-review IMPORTANT #1, T2
#: nexus/code-review-nexus-j9z30-13-2026-09-01 [24062]): every one of
#: these seven branches prints a FIXED constant string (a "remedy" or
#: "tracker not recorded" paragraph) with no run-specific interpolation
#: inside the constant itself -- unlike ``_probe_unverifiable_message()``
#: / ``_print_paired_ack()``, which BUILD their text per call from
#: dynamic arguments and so have no fixed constant to check against.
#: Deliberately imports the constants rather than re-typing them: a
#: hand-transcribed copy is exactly how this drifted the first time
#: (composite_missing_commit's "--" vs the real em dash, caught by this
#: test's own first run).
_STATIC_REMEDY_CONSTANTS: dict[str, str] = {
    "check_pin_currency::pin_currency_stale_pin": _floor._UNPINNED_REMEDY,
    "check_floor_bare::bare_probe_stale_via_exception": _floor._REMEDY,
    "check_floor_bare::bare_probe_stale_via_success": _floor._REMEDY,
    "main_dispatch::main_bare_tracker_opt_out": _floor._TRACKER_OPT_OUT_NOTE,
    "main_dispatch::main_bare_tracker_refusal": _floor._TRACKER_REFUSAL,
    "check_composite::composite_missing_commit": _precond._REMEDY,
    "check_wire_contract_ledger::ledger_blocked": _precond._LEDGER_REMEDY,
}


def test_static_remedy_constants_are_nonempty() -> None:
    """Non-vacuity floor for the content check below: pin that the
    imported constants themselves are real, non-blank text -- a source
    edit that hollowed one out to `""` would otherwise make the
    containment assertion below trivially (and silently) true."""
    assert _STATIC_REMEDY_CONSTANTS
    for row_id, constant_text in _STATIC_REMEDY_CONSTANTS.items():
        assert isinstance(constant_text, str) and constant_text.strip(), row_id


@pytest.mark.parametrize(
    "row_id,constant_text",
    sorted(_STATIC_REMEDY_CONSTANTS.items()),
    ids=sorted(_STATIC_REMEDY_CONSTANTS),
)
def test_message_catalog_contains_static_remedy_constants_verbatim(
    row_id: str, constant_text: str,
) -> None:
    """The catalog-parity CONTENT check (code-review IMPORTANT #1/#3): for
    every row whose printed text is anchored to a fixed module constant,
    the catalog entry must contain that constant's CURRENT value
    byte-for-byte, not a hand-retyped approximation. This is what stops
    RDR-201 P2.4/P2.5 (nexus-j9z30.14/.15) from silently regressing
    operator-facing remedy text when the catalog is eventually wired into
    the two gated scripts -- a paraphrase that reads the same to a human
    would pass the non-emptiness check above but fail this one."""
    assert constant_text in release_messages.RELEASE_MESSAGES[row_id], (
        row_id, constant_text, release_messages.RELEASE_MESSAGES[row_id],
    )


# ---------------------------------------------------------------------------
# Role 2 harness integrity: a corrupted table row must red the harness
# ---------------------------------------------------------------------------
#
# Mirrors Role 1's ``test_a_corrupted_expected_verdict_reds_the_harness``
# above (code-review IMPORTANT #4, T2 nexus/code-review-nexus-j9z30-13
# -2026-09-01 [24062]): Role 2 catches a mistranscribed table row BY
# CONSTRUCTION today (a wrong ``emit.exit_code`` makes ``new != old``), but
# nothing proved that until now -- a Role-2 suite that always passes
# because every authored row happens to already agree with the real
# scripts would look identical to one that is silently incapable of
# catching a wrong row at all.


def _mutated_table_with_flipped_exit_code(row_id: str) -> Table:
    """A copy of the real choreography table with ONE row's
    ``emit.exit_code`` flipped to a value guaranteed different from its
    real one (see ``_a_wrong_exit_code`` above for the identical technique
    Role 1 uses) -- every other row is untouched. ``Row`` and ``Table`` are
    frozen dataclasses, so this rebuilds both via ``dataclasses.replace``
    rather than mutating in place; the real, cached table
    (``_load_choreography_table()``) and the file on disk are never
    touched."""
    table = _load_choreography_table()
    rows = list(table.rows)
    for i, row in enumerate(rows):
        if row.id != row_id:
            continue
        assert isinstance(row.outcome, dict), (row_id, row.outcome)
        corrupted_outcome = dict(row.outcome)
        corrupted_outcome["exit_code"] = str(
            _a_wrong_exit_code(int(row.outcome["exit_code"]))
        )
        rows[i] = dataclasses.replace(row, outcome=corrupted_outcome)
        return dataclasses.replace(table, rows=tuple(rows))
    raise AssertionError(f"no row with id {row_id!r} in the real table")


#: RDR-201 P2.4: a FLOOR-script cell's ``_new_path`` now drives the real
#: gated function (DECISION_PATH flipped), which always reads the REAL
#: table file via its own module-level cache -- it cannot be steered onto
#: an in-memory-only mutated copy. Only the precondition-script branch of
#: ``_new_path`` still honors the ``table=`` override, so this canary must
#: sample a precondition-script cell, not ``_CELL_EVENT_PAIRS[0]`` (a
#: ``check_pin_currency`` -- floor-script -- cell) as before P2.4.
_PRECOND_CELL_EVENT_PAIRS: list[tuple[dict[str, Any], str]] = [
    (cell_dict, event)
    for cell_dict, event in _CELL_EVENT_PAIRS
    if cell_dict["function"] in _PRECOND_SCRIPT_FUNCTIONS
]


def test_a_mutated_table_row_reds_role_2() -> None:
    """Flip one row's ``exit_code`` in an in-memory copy of the real table
    and confirm Role 2's own comparison (``_new_path`` vs
    ``_drive_old_path``) actually disagrees -- proof this harness CAN
    catch a wrong table row, not just a suite that always passes because
    the table happens to already agree with the real scripts. Samples the
    first PRECONDITION-script (cell, event) pair (see
    ``_PRECOND_CELL_EVENT_PAIRS``'s own docstring for why -- a floor-script
    cell's ``_new_path`` no longer honors the ``table=`` override since
    RDR-201 P2.4 wired it to drive the real gated function instead)."""
    assert _PRECOND_CELL_EVENT_PAIRS, "no precondition-script cell in the fixture -- canary cannot run"
    cell_dict, event = _PRECOND_CELL_EVENT_PAIRS[0]
    mutated = _mutated_table_with_flipped_exit_code(cell_dict["cell_id"])
    new = _new_path(cell_dict, event, table=mutated)
    assert new is not NotImplemented
    cell = _cell_from_fixture(cell_dict)
    old = _drive_old_path(cell)
    assert new != old, (
        "the mutated table's flipped exit_code did not change the "
        "resolved verdict -- Role 2's comparison cannot catch a wrong "
        "table row",
        cell_dict["cell_id"], event, old, new,
    )

