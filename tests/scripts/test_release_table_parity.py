# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Parity harness over the enumerated release-decision cells (RDR-201
P2.2 -> P2.6, nexus-j9z30.12/.13/.14/.15/.16).

Loads ``tests/scripts/fixtures/release_cells.json`` (built by
``scripts/enumerate_release_cells.py``, RDR-201 P2.1 / nexus-j9z30.11) and,
for EVERY cell of both gated scripts, pins three things to each other:

Role 1 (``test_real_function_matches_fixture``) drives the REAL gated
function -- via the enumerator's own monkeypatched drivers, never
reimplemented here -- and asserts the observed ``(exit_code, message_key)``
matches the fixture's frozen expected column. Since P2.6 deleted the
pre-table inline branches, the real function IS the table path, so this is
the old-vs-new parity claim P2.4/P2.5 proved cell by cell (verdict and full
printed text, both scripts) now resting on the fixture alone: the fixture
pins the table. Two independent failure modes: (a) a real gated script's
logic drifting from what the fixture recorded, and (b) the checked-in
fixture going STALE relative to ``enumerate_release_cells.py``'s own
modeling (``test_committed_fixture_is_not_stale_relative_to_a_fresh_
enumeration``, which compares ``erc.build_fixture()`` -- pure and
deterministic -- against the committed file).

Role 2 pins the WORDS. ``test_real_function_text_matches_frozen_oracle``
drives the real function capturing stdout and stderr SEPARATELY and
byte-compares each, after normalizing the run-dependent substrings
(``enumerate_release_cells.text_normalizers``), against
``tests/scripts/fixtures/release_cell_texts.json`` -- the text every cell
printed at the P2.6 cutover, when it was proven byte-identical on both
streams to the deleted inline branches (the per-stream probe, independently
re-derived by the .18 code review). That is the P2.4 fix round's byte-for-
byte comparison (which found 23 mismatches, six of them real bugs) kept
alive against a frozen oracle instead of a live one; regeneration is a
deliberate act in the same commit as a message change (see
``build_text_fixture``). ``test_real_function_fills_every_catalog_
placeholder`` is the cheaper companion: no catalog placeholder left
unfilled, the one defect class the catalog design introduced.

Role 3 (``test_table_row_matches_fixture_by_direct_resolve``) resolves the
table directly for every cell's assignment and pins the row's own verdict
(and row id) to the fixture. It is the ONLY coverage the DELEGATING rows
get: a composite/dispatch cell whose real function returns a sub-call's
verdict (``main_dispatch::main_ledger_only_*``,
``check_composite::composite_vacuous_table_ledger_*``,
``precond_main_dispatch::*``) never emits its own row -- the sub-function's
row is the decision -- so Roles 1 and 2 cannot see a wrong exit code on it.
Not a second copy of Role 1: Role 1 proves the wiring, Role 3 proves the
rows.

No event dimension
------------------

RDR-201 P2.3 declared table-wide ``event`` / ``mode`` dimensions and this
harness parametrized Role 2 over every reachable (cell, event) pair,
because Finding 2 had classified the 7.1.0/v0.1.62 incident as an
un-encoded event dimension. Sam's ruling (nexus-j9z30.26, 2026-09-02):
event-invariance is CORRECT. The fix for that incident (79fff05a9)
changed a remedy string and prose, no decision logic; the event-
sensitivity is real but lives in the release / engine-release SKILL
choreography (which moment a human runs which gate), which
``enumerate_release_cells.event_mode_matrix()`` records as citations. A
table over the scripts' own inputs is event-invariant by construction, so
the columns are gone from the table and the pairs are gone from here.
Do not re-derive the question from the incident write-up.

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
import re
from typing import Any, Callable
from unittest.mock import patch

import pytest

import check_client_release_precondition as _precond
import check_engine_release_floor as _floor
import enumerate_release_cells as erc
import release_choreography as _choreo
import release_messages
from nexus.tables.load import Table

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



#: The per-function drivers live in the enumerator (``erc.DRIVERS``) -- one
#: map, shared with the text-oracle writer -- never a copy here.
_DRIVERS = erc.DRIVERS

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


_CORRUPTION_SAMPLE_SEED = 20260902
_CORRUPTION_SAMPLE_SIZE = 3


def _cell_from_fixture(cell_dict: dict[str, Any]) -> erc.Cell:
    return erc.cell_from_dict(cell_dict)


def _drive(cell: erc.Cell) -> tuple[int, str]:
    """Drive the real gated function for one cell through the enumerator's
    own driver for ``cell.function`` -- never reimplemented here; a function
    the fixture names with no driver fails loudly (``KeyError``)."""
    return _DRIVERS[cell.function](cell)


def _drive_capturing_text(cell: erc.Cell) -> tuple[tuple[int, str], str]:
    """``_drive`` plus the combined printed text (stdout then stderr); the
    per-stream form is ``erc.drive_cell_streams``, which this wraps."""
    verdict, out, err = erc.drive_cell_streams(cell)
    return verdict, out + err


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
#: prefix; ``_guard_for`` below applies it). Only functions whose
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


def _guard_for(cell_dict: dict[str, Any]) -> dict[str, str]:
    """The guard a real call site would pass to
    ``release_choreography.resolve_choreography_row`` for ``cell_dict``:
    the fixture's inputs, routed through ``_SYNTHETIC_GUARD_TRANSFORMS`` for
    the one function whose table dimension is not a rename of its input
    keys, with the enumerator's ``"n/a"`` sentinel dropped (see
    ``_NOT_APPLICABLE``). Nothing else -- the function-prefixing and the
    fill-every-other-dimension rule are production's, in
    ``resolve_choreography_row``, and this harness must not carry a second
    copy of them (the .18 critique found exactly that copy here)."""
    function = cell_dict["function"]
    transform = _SYNTHETIC_GUARD_TRANSFORMS.get(function)
    raw_inputs = transform(cell_dict["inputs"]) if transform else cell_dict["inputs"]
    return {key: value for key, value in raw_inputs.items() if value != _NOT_APPLICABLE}


def _assert_real_function_matches_fixture(cell_dict: dict[str, Any]) -> None:
    cell = _cell_from_fixture(cell_dict)
    observed = _drive(cell)
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
    ``_first_cell_per_script`` would ``KeyError`` deep inside test
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
def test_real_function_matches_fixture(cell_dict: dict[str, Any]) -> None:
    """Role 1 -- see the module docstring."""
    _assert_real_function_matches_fixture(cell_dict)


# ---------------------------------------------------------------------------
# Role 2: the real function fills every catalog placeholder it prints
# ---------------------------------------------------------------------------

#: Bracketed tokens in release_messages.py that are PROSE, not placeholders
#: (the ``[additive]`` direction-safety token is quoted verbatim in four
#: ledger messages). Anything else of the shape ``[name]`` in a catalog
#: entry is a hole a call site must fill.
_LITERAL_BRACKET_TOKENS = frozenset({"[additive]"})
_BRACKET_TOKEN = re.compile(r"\[[A-Za-z0-9_.]+\]")


def _catalog_placeholders() -> frozenset[str]:
    tokens: set[str] = set()
    for text in release_messages.RELEASE_MESSAGES.values():
        tokens.update(_BRACKET_TOKEN.findall(text))
    return frozenset(tokens - _LITERAL_BRACKET_TOKENS)


_CATALOG_PLACEHOLDERS = _catalog_placeholders()


def _unfilled_placeholders(text: str, placeholders: frozenset[str] = _CATALOG_PLACEHOLDERS) -> list[str]:
    return sorted(p for p in placeholders if p in text)


def test_catalog_declares_placeholders() -> None:
    """Non-vacuity for Role 2: a catalog with no placeholders would make
    every case below pass without checking anything."""
    assert len(_CATALOG_PLACEHOLDERS) >= 20, sorted(_CATALOG_PLACEHOLDERS)


@pytest.mark.parametrize("cell_dict", _FIXTURE_CELLS, ids=_FIXTURE_CELL_IDS)
def test_real_function_fills_every_catalog_placeholder(cell_dict: dict[str, Any]) -> None:
    """Role 2 -- see the module docstring. Checked against the union of
    every catalog entry's placeholders, not just the cell's own row's: a
    delegating cell prints its DELEGATE's entry, and that is the text
    whose holes matter."""
    cell = _cell_from_fixture(cell_dict)
    verdict, text = _drive_capturing_text(cell)
    assert verdict == (cell.exit_code, cell.message_key), (cell_dict["cell_id"], verdict)
    unfilled = _unfilled_placeholders(text)
    assert not unfilled, (cell_dict["cell_id"], unfilled, text)


def test_placeholder_check_catches_an_unfilled_placeholder() -> None:
    """Proof Role 2 CAN fail: plant an extra placeholder in one real cell's
    catalog entry -- nothing at the call site knows to fill it -- and
    confirm the check reports it."""
    cell_dict = _FIXTURE_CELLS[0]
    row_id = cell_dict["cell_id"]
    planted = release_messages.RELEASE_MESSAGES[row_id] + " [probe_hole]"
    with patch.dict(release_messages.RELEASE_MESSAGES, {row_id: planted}):
        _, text = _drive_capturing_text(_cell_from_fixture(cell_dict))
        planted_placeholders = _catalog_placeholders()
    assert "[probe_hole]" in text, (row_id, text)
    assert "[probe_hole]" in _unfilled_placeholders(text, planted_placeholders), "the predicate itself must report the planted hole"


_TEXT_FIXTURE_PATH = _FIXTURE_PATH.with_name("release_cell_texts.json")
_TEXT_FIXTURE: dict[str, dict[str, str]] = json.loads(_TEXT_FIXTURE_PATH.read_text(encoding="utf-8"))


def test_text_oracle_covers_every_cell_and_is_not_empty() -> None:
    assert set(_TEXT_FIXTURE) == set(_FIXTURE_CELL_IDS), (
        "release_cell_texts.json is out of step with release_cells.json -- regenerate "
        "both together (enumerate_release_cells.py --out ... --texts-out ...)"
    )
    non_empty = sum(1 for t in _TEXT_FIXTURE.values() if t["out"] or t["err"])
    assert non_empty >= 80, non_empty
    literals = [lit for lit, _tok in erc.text_normalizers()]
    for cell_id, streams in _TEXT_FIXTURE.items():
        for literal in literals:
            assert literal not in streams["out"] + streams["err"], (cell_id, literal, "un-normalized run-dependent value in the oracle")
    assert any("<FLOOR>" in t["out"] + t["err"] for t in _TEXT_FIXTURE.values())


@pytest.mark.parametrize("cell_dict", _FIXTURE_CELLS, ids=_FIXTURE_CELL_IDS)
def test_real_function_text_matches_frozen_oracle(cell_dict: dict[str, Any]) -> None:
    """Role 2, the words -- see the module docstring. stdout and stderr
    compared separately. A red here is either a regression in the catalog
    or a call site's substitutions (fix the code), or a deliberate message
    change (regenerate the oracle in the same commit and review its diff);
    never a reason to loosen this comparison."""
    verdict, out, err = erc.drive_cell_streams(_cell_from_fixture(cell_dict))
    assert verdict == (cell_dict["exit_code"], cell_dict["message_key"]), (cell_dict["cell_id"], verdict)
    observed = {"out": erc.normalize_text(out), "err": erc.normalize_text(err)}
    assert observed == _TEXT_FIXTURE[cell_dict["cell_id"]], cell_dict["cell_id"]


def test_choreography_table_declares_no_event_or_mode_dimension() -> None:
    """Ruling nexus-j9z30.26 (Sam, 2026-09-02): the choreography is
    event-invariant; the table-wide event/mode columns P2.3 declared were
    removed at P2.6. The checker's unused-dimension finding is a
    non-blocking advisory, so this is the mechanical half of the table
    header's "do not re-add" paragraph."""
    declared = set(_choreo.choreography_table().dimensions)
    assert not declared & {"event", "mode"}, sorted(declared & {"event", "mode"})


# ---------------------------------------------------------------------------
# Role 3: every table row (delegating rows included) pins to the fixture
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cell_dict", _FIXTURE_CELLS, ids=_FIXTURE_CELL_IDS)
def test_table_row_matches_fixture_by_direct_resolve(cell_dict: dict[str, Any]) -> None:
    """Resolve the table through PRODUCTION's own resolver
    (``release_choreography.resolve_choreography_row``, fed the guard a real
    call site would pass) and pin the row it hits -- id, exit code, message
    key -- to the fixture. See the module docstring's Role 3: the only
    place a DELEGATING row's declared verdict is checked (no real function
    reads it), and the only check on the seven ``record_deploy`` cells whose
    driver classifier is a label lookup. A refusal on a cell the fixture
    enumerated as reachable is a table-authoring defect and fails loudly."""
    function = cell_dict["function"]
    guard = _guard_for(cell_dict)
    try:
        row = _choreo.resolve_choreography_row(function, guard)
    except _choreo.TableDefect as exc:
        raise AssertionError(f"{cell_dict['cell_id']}: {exc}") from exc
    outcome = row.outcome
    assert isinstance(outcome, dict), (cell_dict["cell_id"], row.id, outcome)
    assert row.id == cell_dict["cell_id"]
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
        _assert_real_function_matches_fixture(corrupted)


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
#: inside the constant itself -- unlike the per-call message builders the
#: old path had (deleted at P2.6; their text lives only in the catalog now), which BUILT their text per call from
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
# Harness integrity: the real function must FOLLOW the table
# ---------------------------------------------------------------------------
#
# Mirrors Role 1's ``test_a_corrupted_expected_verdict_reds_the_harness``
# above (code-review IMPORTANT #4, T2 nexus/code-review-nexus-j9z30-13
# -2026-09-01 [24062]): a suite that always passes because every authored
# row happens to already agree with the real scripts would look identical
# to one where the scripts silently stopped reading the table at all.


def _first_cell_per_script() -> list[tuple[str, dict[str, Any]]]:
    """One (script, cell) sample per gated script -- the first cell of each
    in fixture order. Both scripts must be represented: a canary that only
    ever samples one script cannot tell that the OTHER script's decision
    path silently ignores the table (an emit reading some other cache, a
    branch that still prints its own verdict)."""
    seen: dict[str, dict[str, Any]] = {}
    for cell_dict in _FIXTURE_CELLS:
        seen.setdefault(_FUNCTION_SCRIPT[cell_dict["function"]], cell_dict)
    scripts = sorted(set(_FUNCTION_SCRIPT.values()))
    missing = [name for name in scripts if name not in seen]
    assert not missing, f"no fixture cell for {missing} -- the canary cannot cover that script"
    return [(name, seen[name]) for name in scripts]


_MUTATION_CANARY_SAMPLES = _first_cell_per_script()


@pytest.mark.parametrize(
    "script,cell_dict", _MUTATION_CANARY_SAMPLES,
    ids=[f"{script}:{cell_dict['cell_id']}" for script, cell_dict in _MUTATION_CANARY_SAMPLES],
)
def test_a_mutated_table_row_changes_the_real_verdict(
    script: str, cell_dict: dict[str, Any], mutate_choreography_row: Callable[[str, int], Table],
) -> None:
    """Flip one row's ``exit_code`` in an in-memory copy of the real table,
    steer the REAL gated function onto that copy by patching
    ``release_choreography.choreography_table`` -- the ONE accessor every
    emit in either script resolves through (nexus-w2x5x) -- and confirm the
    observed verdict follows the mutation. Proof the production wiring
    reads the table, not just a suite that agrees with it by coincidence.

    Parametrized over one cell per gated script. The file on disk and the
    real cached table are never touched: the mutated copy
    (``mutate_choreography_row``, tests/scripts/conftest.py) is built
    BEFORE the patch, from the real accessor, and the patch is scoped to
    the one driven call."""
    cell = _cell_from_fixture(cell_dict)
    expected = (cell.exit_code, cell.message_key)
    mutated = mutate_choreography_row(cell_dict["cell_id"], _a_wrong_exit_code(cell.exit_code))
    with patch.object(_choreo, "choreography_table", return_value=mutated):
        observed = _drive(cell)
    assert observed != expected, (
        "the mutated table's flipped exit_code did not change the real "
        "function's verdict -- this script's decision path is not reading "
        "the table",
        script, cell_dict["cell_id"], expected, observed,
    )
