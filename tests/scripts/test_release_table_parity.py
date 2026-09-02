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

Three roles
-----------

Role 1 (``test_old_path_matches_fixture``) is a CHANGE DETECTOR for the
enumeration itself, with two independent
failure modes: (a) a real gated script's branch logic drifting from what
the fixture recorded (each cell's driver re-executes the real function),
and (b) the checked-in fixture going STALE relative to
``enumerate_release_cells.py``'s own modeling -- e.g. someone edits a cell
table in the enumerator but forgets to regenerate the JSON
(``test_committed_fixture_is_not_stale_relative_to_a_fresh_enumeration``
below, which compares ``erc.build_fixture()`` -- a pure, deterministic
function -- against the committed file directly).

Role 2 (``test_new_path_matches_old_path_cell_by_cell``) drives the REAL
gated function twice per cell -- ``release_choreography.DECISION_PATH``
"old", then "table" -- and asserts the classified verdict AND the full
printed text agree. Since RDR-201 P2.5 (nexus-j9z30.15) that covers BOTH
scripts' twelve cell-producing functions; no cell resolves the table in
isolation any more.

Role 3 (``test_table_row_matches_fixture_by_direct_resolve``) resolves the
table directly for every cell's assignment and pins the row's own verdict
(and row id) to the fixture. It is the ONLY coverage the DELEGATING rows
get: a composite/dispatch cell whose real function prints nothing itself
and returns a sub-call's verdict (``main_dispatch::main_ledger_only_*``,
``check_composite::composite_vacuous_table_ledger_*``,
``precond_main_dispatch::*``) never emits its own row under the table
path -- the sub-function's row is the decision -- so Role 2 cannot see a
wrong exit code on it. Not a second copy of Role 2: Role 2 proves the
wiring, Role 3 proves the rows.

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
import json
import pathlib
import random
from typing import Any, Callable
from unittest.mock import patch

import pytest

import check_client_release_precondition as _precond
import check_engine_release_floor as _floor
import enumerate_release_cells as erc
import release_choreography as _choreo
import release_messages
from nexus.tables.load import Table
from nexus.tables.resolve import resolve

_FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "release_cells.json"
)
_FIXTURE: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
_FIXTURE_CELLS: list[dict[str, Any]] = _FIXTURE["cells"]
_FIXTURE_CELL_IDS: list[str] = [c["cell_id"] for c in _FIXTURE_CELLS]

#: The choreography table (RDR-201 P2.3, nexus-j9z30.13), read through the
#: SAME path constant and the SAME lru_cache'd accessor
#: (``release_choreography.choreography_table``) both real gated scripts
#: resolve through (nexus-w2x5x) -- this harness never loads a private
#: copy the scripts cannot see.
_CHOREOGRAPHY_TABLE_PATH = _choreo.CHOREOGRAPHY_TABLE_PATH

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


def _drive_capturing_full_text(function: str, cell: erc.Cell, decision_path: str) -> tuple[tuple[int, str], str]:
    """RDR-201 P2.4 fix round (critique T2 nexus/critique-nexus-j9z30-14
    -2026-09-02 [24073] finding (a)): drive ``function``'s real gated call
    for ``cell`` with ``release_choreography.DECISION_PATH`` set to
    ``decision_path`` ("old" or "table"), returning BOTH the classified
    ``(exit_code, message_key)`` verdict AND the full, combined stdout+stderr
    TEXT the call produced.

    Every existing comparison in this file (Role 1, Role 2, the fixture
    itself) reduces a driven call to ``(exit_code, message_key)`` via
    ``enumerate_release_cells.py``'s own marker-substring classifiers --
    never the actual printed prose. A content-level regression in
    ``release_messages.py`` (a dropped caveat, a wrong remedy pointer, a
    hollowed-out placeholder) that happens to preserve both the exit code
    and whatever substring a classifier keys on is invisible to all of
    that. This spies on ``enumerate_release_cells._capture`` -- the SINGLE
    choke point every ``drive_*`` function funnels its real call through --
    to record the raw text alongside the classified result, without
    reimplementing any driver's own sensor-patching.
    """
    original_capture = erc._capture
    chunks: list[str] = []

    def _spy_capture(fn: Callable[..., int], *args: Any, **kwargs: Any) -> tuple[int, str, str]:
        rc, out, err = original_capture(fn, *args, **kwargs)
        chunks.append(out + err)
        return rc, out, err

    original_path = _choreo.DECISION_PATH
    _choreo.DECISION_PATH = decision_path
    try:
        with patch.object(erc, "_capture", side_effect=_spy_capture):
            verdict = _OLD_PATH_DRIVERS[function](cell)
    finally:
        _choreo.DECISION_PATH = original_path
    return verdict, "".join(chunks)


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


def _new_path(cell: dict[str, Any], event: str) -> tuple[int, str]:
    """RDR-201 P2.4/P2.5 (nexus-j9z30.14/.15): drive the REAL gated
    function for ``cell`` with ``release_choreography.DECISION_PATH``
    flipped to ``"table"`` -- exercising the ACTUAL production wiring
    (``release_choreography.emit_choreography()`` as called from inside
    whichever of the two scripts owns ``cell["function"]``), never the
    table in isolation. Reuses ``_OLD_PATH_DRIVERS[function]`` unchanged:
    the same driver monkeypatches the sensors and invokes the real gated
    function either way, so which decision path fires depends only on the
    flag. The table is read through ``release_choreography
    .choreography_table()`` -- the ONE accessor -- so a test that patches
    that name steers the real function (nexus-w2x5x).

    ``event`` is carried for parity with the (cell, event) parametrization
    Role 2 still runs under (see the module docstring); the real function
    has no event input, so nothing here consults it."""
    driven_cell = _cell_from_fixture(cell)
    original_path = _choreo.DECISION_PATH
    _choreo.DECISION_PATH = "table"
    try:
        return _OLD_PATH_DRIVERS[cell["function"]](driven_cell)
    finally:
        _choreo.DECISION_PATH = original_path


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

#: RDR-201 P2.4 fix round (critique T2 nexus/critique-nexus-j9z30-14
#: -2026-09-02 [24073] finding (a); code-review T2
#: nexus/code-review-nexus-j9z30-14-2026-09-02 [24074] Important 1):
#: cell_id -> reason, for a cell whose OLD-path and NEW-path
#: printed TEXT is legitimately, permanently unequal even though their
#: (exit_code, message_key) verdict agrees. As of this fix round: EMPTY --
#: release_messages.py's catalog entries were corrected (ledger-path
#: placeholders, the newline-joined blocked-entries layout, the six
#: tracker_* entries' hardcoded exception paraphrase replaced with the real
#: [exc] placeholder, the two auto-paired UNVERIFIABLE messages' dropped
#: caveat clause, and the four PAIRED MODE ack messages' missing POST-TAG
#: VERIFY paragraph) until every floor-script cell in the fixture produced
#: byte-identical text on both paths. A future addition here must name the
#: specific cell_id and the reason full parity is impossible for it, never
#: widen silently (e.g. via a blanket skip on a whole function).
_FULL_TEXT_NAMED_EXCEPTIONS: dict[str, str] = {}


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
    this test resolves for itself by declaring one interpretation correct.

    RDR-201 P2.4 fix round (critique T2 nexus/critique-nexus-j9z30-14
    -2026-09-02 [24073] finding (a)): ALSO drive both paths a second time
    capturing the full printed TEXT (not just the classified verdict) and
    assert byte equality -- closing the blind spot where a content-level
    regression in release_messages.py's prose could preserve the exit code
    and whatever substring a classifier keys on while still being wrong.
    Since RDR-201 P2.5 (nexus-j9z30.15) this covers every cell of BOTH
    scripts -- the precondition script's three cell-producing functions
    drive real printed output through the same switch now."""
    new = _new_path(cell_dict, event)
    cell = _cell_from_fixture(cell_dict)
    old = _drive_old_path(cell)
    assert new == old, (cell_dict["cell_id"], event, old, new)

    function = cell_dict["function"]
    old_verdict, old_text = _drive_capturing_full_text(function, cell, "old")
    new_verdict, new_text = _drive_capturing_full_text(function, cell, "table")
    assert old_verdict == new_verdict, (cell_dict["cell_id"], event, old_verdict, new_verdict)
    cell_id = cell_dict["cell_id"]
    if cell_id in _FULL_TEXT_NAMED_EXCEPTIONS:
        pytest.skip(_FULL_TEXT_NAMED_EXCEPTIONS[cell_id])
    assert old_text == new_text, (cell_id, event, old_text, new_text)


# ---------------------------------------------------------------------------
# Role 3: every table row (delegating rows included) pins to the fixture
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cell_dict", _FIXTURE_CELLS, ids=_FIXTURE_CELL_IDS)
def test_table_row_matches_fixture_by_direct_resolve(cell_dict: dict[str, Any]) -> None:
    """Resolve the table directly for ``cell_dict``'s own assignment and
    pin the row it hits -- id, exit code, message key -- to the fixture.
    See the module docstring's Role 3: this is the only place a DELEGATING
    row's verdict is checked at all, since no real function ever emits
    one. Event-invariant by ruling (nexus-j9z30.26), so one event per cell
    suffices; a refusal on a cell the fixture enumerated as reachable is a
    table-authoring defect and fails loudly with the assignment named."""
    table = _choreo.choreography_table()
    event = _cell_reachable_events(cell_dict)[0]
    assignment = _assignment_for(table, cell_dict, event)
    resolution = resolve(table, assignment)
    assert resolution.refusal is None, (
        f"{cell_dict['cell_id']}: table refused a cell the fixture enumerated "
        f"as reachable -- {resolution.refusal} {dict(resolution.detail)} "
        f"(assignment={assignment})"
    )
    outcome = resolution.row.outcome
    assert isinstance(outcome, dict), (cell_dict["cell_id"], resolution.row.id, outcome)
    assert resolution.row.id == cell_dict["cell_id"]
    assert (int(outcome["exit_code"]), outcome["message_key"]) == (
        cell_dict["exit_code"], cell_dict["message_key"],
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
    table = _choreo.choreography_table()
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
# Catalog <-> classifier coupling (RDR-201 P2.4 fix round, critique T2
# nexus/critique-nexus-j9z30-14-2026-09-02 [24073] finding (c))
# ---------------------------------------------------------------------------
#
# release_messages.py's battery_not_published / battery_published_unavailable
# entries carry no fixed marker text of their own -- see the COUPLING
# comments on both ends (release_messages.py's two entries,
# enumerate_release_cells._classify_paired_preconditions). These tests pin
# the coupling DIRECTLY against the real classifier function (never a
# hand-retyped duplicate of its marker strings, which would just move the
# drift risk into the test itself): fill each catalog TEMPLATE's [reason]
# placeholder with a representative reason and confirm the REAL classifier
# still recognizes it. A future edit that removes the placeholder, or
# rewords _classify_paired_preconditions's markers out of step with
# check_engine_release_floor._paired_tag_published()'s own reason strings,
# fails HERE -- precisely and locally -- rather than as a cryptic
# AssertionError deep inside the 200-case parametrized Role-2 suite.


def test_battery_not_published_catalog_reason_placeholder_drives_the_classifier() -> None:
    template = release_messages.RELEASE_MESSAGES["check_paired_preconditions::battery_not_published"]
    assert "[reason]" in template, "the substitution point itself must survive"
    filled = template.replace("[reason]", "release engine-service-vTEST is still a DRAFT -- not published")
    assert erc._classify_paired_preconditions(1, filled) == "battery_not_published"


def test_battery_published_unavailable_catalog_reason_placeholder_drives_the_classifier() -> None:
    template = release_messages.RELEASE_MESSAGES["check_paired_preconditions::battery_published_unavailable"]
    assert "[reason]" in template, "the substitution point itself must survive"
    filled = template.replace("[reason]", "gh unavailable")
    assert erc._classify_paired_preconditions(2, filled) == "battery_published_unavailable"


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


def _first_cell_event_pair_per_script() -> list[tuple[str, dict[str, Any], str]]:
    """One (script, cell, event) sample per gated script -- the first
    reachable pair of each in fixture order. Both scripts must be
    represented: a canary that only ever samples one script cannot tell
    that the OTHER script's table path silently ignores the table (e.g. a
    switch flipped on a module that no longer owns it)."""
    seen: dict[str, tuple[dict[str, Any], str]] = {}
    for cell_dict, event in _CELL_EVENT_PAIRS:
        seen.setdefault(_FUNCTION_SCRIPT[cell_dict["function"]], (cell_dict, event))
    scripts = sorted(set(_FUNCTION_SCRIPT.values()))
    missing = [name for name in scripts if name not in seen]
    assert not missing, f"no fixture cell for {missing} -- the canary cannot cover that script"
    return [(name, *seen[name]) for name in scripts]


_MUTATION_CANARY_SAMPLES = _first_cell_event_pair_per_script()


@pytest.mark.parametrize(
    "script,cell_dict,event", _MUTATION_CANARY_SAMPLES,
    ids=[f"{script}:{cell_dict['cell_id']}@{event}" for script, cell_dict, event in _MUTATION_CANARY_SAMPLES],
)
def test_a_mutated_table_row_reds_role_2(
    script: str, cell_dict: dict[str, Any], event: str, mutate_choreography_row: Callable[[str, int], Table],
) -> None:
    """Flip one row's ``exit_code`` in an in-memory copy of the real table,
    steer the REAL gated function onto that copy by patching
    ``release_choreography.choreography_table`` -- the ONE accessor every
    table-path emit in either script resolves through (nexus-w2x5x) -- and
    confirm Role 2's own comparison (``_new_path`` vs ``_drive_old_path``)
    actually disagrees. Proof this harness CAN catch a wrong table row on
    the production wiring itself, not just a suite that always passes
    because the table happens to already agree with the real scripts.

    Parametrized over one cell per gated script. A script whose table path
    is not genuinely live (its switch stale, its emit reading some other
    cache) ignores the mutation, returns the old verdict, and REDS here --
    which is the whole reason this samples both scripts, not just one.
    The file on disk and the real cached table are never touched: the
    mutated copy (``mutate_choreography_row``, tests/scripts/conftest.py)
    is built BEFORE the patch, from the real accessor, and the patch is
    scoped to the one ``_new_path`` call."""
    mutated = mutate_choreography_row(cell_dict["cell_id"], _a_wrong_exit_code(cell_dict["exit_code"]))
    with patch.object(_choreo, "choreography_table", return_value=mutated):
        new = _new_path(cell_dict, event)
    cell = _cell_from_fixture(cell_dict)
    old = _drive_old_path(cell)
    assert new != old, (
        "the mutated table's flipped exit_code did not change the "
        "resolved verdict -- Role 2's comparison cannot catch a wrong "
        "table row on this script's table path",
        script, cell_dict["cell_id"], event, old, new,
    )
