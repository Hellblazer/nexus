"""Tests for nexus.tables.resolve (RDR-201 P1.2).

resolve(table, assignment) returns the single matching Row for a concrete
assignment of every declared dimension, or a typed refusal drawn from the
closed set {no-match, ambiguous-match, unknown-value}. The evaluator never
breaks a tie -- ambiguity at runtime is a defect the checker should have
caught, reported by naming every candidate row id.

Outcome-kind coverage note: the real rdr-lifecycle.toml (state-machine
kind) only ever authors `to` / `refuse` outcomes -- a state machine has no
`emit` rows by construction. The `emit`-kind hit test therefore uses
tests/fixtures/tables/release_decision_clean.toml (decision-table kind)
instead; every other named scenario in the bead spec (draft+accept+...,
deferred+resume, accepted+defer, closed+close) is exercised against the
real packaged lifecycle table.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from nexus.tables.check import check_table, exit_code
from nexus.tables.load import Dimension, Row, Table, TableLoadError, load_packaged_table, load_table
from nexus.tables.resolve import (
    AMBIGUOUS_MATCH,
    NO_MATCH,
    UNKNOWN_VALUE,
    Resolution,
    resolve,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "tables"
PACKAGED_TABLES = Path(__file__).resolve().parent.parent.parent / "src" / "nexus" / "tables"


# --------------------------------------------------------------------------
# Evaluator refusals -- one test per typed code, {no-match, ambiguous-match,
# unknown-value}. unknown-value has two distinct triggers (missing
# dimension, out-of-domain value) per the bead's own wording, both covered.


def test_missing_dimension_is_unknown_value():
    table = load_packaged_table("rdr-lifecycle.toml")
    assignment = {"status": "draft", "event": "accept", "gate": "passed"}  # successor omitted
    res = resolve(table, assignment)
    assert res.row is None
    assert res.refusal == UNKNOWN_VALUE
    assert res.detail["dimension"] == "successor"
    assert res.detail["reason"] == "missing"


def test_out_of_domain_value_is_unknown_value():
    table = load_packaged_table("rdr-lifecycle.toml")
    assignment = {"status": "bogus", "event": "accept", "gate": "passed", "successor": "absent"}
    res = resolve(table, assignment)
    assert res.row is None
    assert res.refusal == UNKNOWN_VALUE
    assert res.detail["dimension"] == "status"
    assert res.detail["value"] == "bogus"
    assert res.detail["reason"] == "out-of-domain"


def test_zero_candidates_and_no_escape_row_is_no_match():
    """A hand-built table with an ordinary row that does not cover the
    whole domain and NO escape row at all: the uncovered assignment must
    refuse no-match, not silently pick something."""
    table = Table(
        id="no-match-fixture",
        kind="decision-table",
        dimensions={
            "decision": Dimension(name="decision", domain=("decide",)),
            "tier": Dimension(name="tier", domain=("free", "paid")),
        },
        match_keys=("decision",),
        rows=(
            Row(
                id="only-free",
                match={"decision": "decide"},
                guard={"tier": ("free",)},
                outcome_kind="emit",
                outcome={"verdict": "ok"},
                escape=False,
            ),
        ),
    )
    res = resolve(table, {"decision": "decide", "tier": "paid"})
    assert res.row is None
    assert res.refusal == NO_MATCH


# --------------------------------------------------------------------------
# ambiguous-match, and its inverse non-vacuity: overlap_no_guard.toml is a
# table the CHECKER flags (OVERLAP, blocking) -- resolve() on the exact
# shared cell must independently report ambiguous-match naming both rows,
# proving the evaluator's own ambiguity detection is live, not inherited
# from the checker having already screened the input.


def test_ambiguous_match_names_every_candidate_row_id():
    table = load_table(FIXTURES / "overlap_no_guard.toml")
    # Sanity: the checker itself flags this table (non-vacuity anchor).
    assert exit_code(check_table(table)) == 1

    res = resolve(table, {"status": "draft", "event": "accept"})
    assert res.row is None
    assert res.refusal == AMBIGUOUS_MATCH
    assert res.detail["candidates"] == ["accept-a", "accept-b"]


# --------------------------------------------------------------------------
# Hit tests, one per outcome kind (to / emit / refuse), plus the escape
# variant, against the real packaged lifecycle table (state-machine) and
# the release-floor decision fixture (decision-table, for `emit`).


def test_hit_to_outcome_draft_accept_gate_passed():
    table = load_packaged_table("rdr-lifecycle.toml")
    res = resolve(table, {"status": "draft", "event": "accept", "gate": "passed", "successor": "absent"})
    assert res.refusal is None
    assert res.escaped is False
    assert res.row.outcome_kind == "to"
    assert res.row.outcome == {"status": "accepted"}


def test_hit_refuse_outcome_draft_accept_gate_none():
    table = load_packaged_table("rdr-lifecycle.toml")
    res = resolve(table, {"status": "draft", "event": "accept", "gate": "none", "successor": "absent"})
    assert res.refusal is None
    assert res.escaped is False
    assert res.row.outcome_kind == "refuse"
    assert res.row.outcome == "gate-not-passed"


def test_hit_deferred_resume_to_draft():
    table = load_packaged_table("rdr-lifecycle.toml")
    res = resolve(table, {"status": "deferred", "event": "resume", "gate": "none", "successor": "absent"})
    assert res.refusal is None
    assert res.escaped is False
    assert res.row.outcome_kind == "to"
    assert res.row.outcome == {"status": "draft"}


def test_hit_accepted_defer_to_deferred():
    table = load_packaged_table("rdr-lifecycle.toml")
    res = resolve(table, {"status": "accepted", "event": "defer", "gate": "none", "successor": "absent"})
    assert res.refusal is None
    assert res.escaped is False
    assert res.row.outcome_kind == "to"
    assert res.row.outcome == {"status": "deferred"}


def test_hit_closed_close_is_escaped_illegal_transition():
    table = load_packaged_table("rdr-lifecycle.toml")
    res = resolve(table, {"status": "closed", "event": "close", "gate": "none", "successor": "absent"})
    assert res.refusal is None
    assert res.escaped is True
    assert res.row.outcome_kind == "refuse"
    assert res.row.outcome == "illegal-transition"


def test_hit_emit_outcome_release_decision_unreachable():
    table = load_table(FIXTURES / "release_decision_clean.toml")
    res = resolve(
        table,
        {"decision": "decide", "cloud_vs_floor": "unreachable", "mode": "bare", "ledger": "none"},
    )
    assert res.refusal is None
    assert res.escaped is False
    assert res.row.outcome_kind == "emit"
    assert res.row.outcome == {"verdict": "block"}


# --------------------------------------------------------------------------
# Property: a table the checker calls clean (no blocking finding) must be
# TOTAL over its full declared product -- resolve() can never return
# ambiguous-match or no-match for any combination of every declared
# dimension's domain.
#
# Correctly attributed (RDR-201 P1.2 critique, T2
# nexus/critique-nexus-j9z30-2-2026-09-01 [24018]): check_table's per-group
# coverage/overlap proof used to be scoped to EXISTING match groups only --
# a match-key value combination no row ever named had no group at all, so
# nothing was checked, and a checker-clean table was not actually
# guaranteed total (counter-example in the critique: three declared
# statuses, rows naming only two). check_table now ALSO proves every
# match-key combination is named by some row (`unmatched-assignment`,
# src/nexus/tables/check.py's _check_match_totality) before per-group
# coverage is even asked about, so "checker-clean implies resolve()-total"
# is true by construction again, not true of today's fixtures by luck.
#
# This test remains a genuine INDEPENDENT cross-check rather than a mere
# restatement, because resolve.py and check.py share no acceptance-predicate
# code (resolve.py imports only from load.py, never from check.py) -- this
# test re-derives totality empirically via itertools.product + the real
# resolve(), so a bug in either module's algorithm that the other's does not
# happen to share would still be caught here.


def _load_clean_tables() -> list[tuple[str, Table]]:
    paths = sorted(PACKAGED_TABLES.glob("*.toml")) + sorted(FIXTURES.glob("*.toml"))
    clean: list[tuple[str, Table]] = []
    for path in paths:
        try:
            table = load_table(path)
        except TableLoadError:
            continue  # load-refusal fixtures are not tables to check at all
        if exit_code(check_table(table)) == 0:
            clean.append((path.name, table))
    return clean


CLEAN_TABLES = _load_clean_tables()


def test_clean_table_discovery_is_non_vacuous():
    """nexus-moht0 doctrine: a sweep that found nothing to check is a
    failure, not a pass. Also pins the real production table's presence by
    name, not just a bare count -- a silent regression that dropped
    rdr-lifecycle.toml out of "clean" would otherwise just shrink this list
    without failing anything."""
    assert len(CLEAN_TABLES) >= 3, "expected multiple clean fixtures/tables to exercise the property test"
    assert "rdr-lifecycle.toml" in {name for name, _ in CLEAN_TABLES}


@pytest.mark.parametrize("name,table", CLEAN_TABLES, ids=[name for name, _ in CLEAN_TABLES])
def test_resolve_is_total_over_clean_table_full_declared_product(name, table):
    dims = sorted(table.dimensions)
    for dim_name in dims:
        dim = table.dimensions[dim_name]
        assert dim.kind == "enum" and dim.domain, (
            f"table {name!r} declares non-enumerable dimension {dim_name!r}; "
            "a clean table's dimensions must all be finite enums for this property to be checkable"
        )

    domains = [table.dimensions[d].domain for d in dims]
    for combo in itertools.product(*domains):
        assignment = dict(zip(dims, combo))
        res = resolve(table, assignment)
        assert res.refusal not in (AMBIGUOUS_MATCH, NO_MATCH), (name, assignment, res)


def test_resolve_no_match_on_checker_reported_unmatched_assignment():
    """The property's inverse: unmatched_assignment.toml is a table the
    checker itself flags (unmatched-assignment, blocking) -- resolve() on
    the exact cell no row names (status=c, event=go) must independently
    refuse no-match, proving the evaluator's own behavior on an
    unmatched-assignment cell matches what the checker warned about."""
    table = load_table(FIXTURES / "unmatched_assignment.toml")
    assert exit_code(check_table(table)) == 1  # non-vacuity anchor

    res = resolve(table, {"status": "c", "event": "go"})
    assert res.row is None
    assert res.refusal == NO_MATCH


# --------------------------------------------------------------------------
# Resolution's own either/or invariant.


def test_resolution_requires_exactly_one_of_row_or_refusal():
    with pytest.raises(ValueError):
        Resolution()
    with pytest.raises(ValueError):
        Resolution(
            row=Row(
                id="x",
                match={},
                guard={},
                outcome_kind="refuse",
                outcome="x",
                escape=False,
            ),
            refusal=NO_MATCH,
        )


def test_resolution_is_actually_hashable():
    """Code review finding (T2 nexus/code-review-nexus-j9z30-2-2026-09-01):
    Resolution must call hash(), not merely be a frozen dataclass -- a
    frozen dataclass with a plain-dict field is NOT hashable at runtime
    despite dataclass-generating __hash__/__eq__ by default (same trap
    Row/Finding already guard against for outcome/detail)."""
    hit = Resolution(
        row=Row(id="x", match={}, guard={}, outcome_kind="to", outcome={"state": "y"}, escape=False),
    )
    assert hash(hit) == hash(hit)  # does not raise

    refusal = Resolution(refusal=NO_MATCH, detail={"nested": ["candidates", "here"]})
    assert hash(refusal) == hash(refusal)  # does not raise, even with a list-valued detail
