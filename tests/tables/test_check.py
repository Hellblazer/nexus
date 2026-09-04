"""Tests for nexus.tables.load / nexus.tables.check (RDR-201 P1.1).

Ported from the enumcheck prototype's 17 tests
(tests/fixtures/tables/_prototype/tests/test_checker.py) onto the
production nexus.tables API, plus the load-refusal tests the production
schema adds (match-keys-mismatch, multiple-outcomes,
multiple-escapes-in-group) and a non-vacuity pair proving the planted
overlap/gap in the defect fixtures are caught by live detection code, not
an accidentally-passing assertion.

Non-vacuity discipline (carried over from the prototype): every
"planted defect" test asserts the EXACT finding code(s) and detail
values expected, not merely "findings is non-empty".
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from nexus.tables import check as check_mod
from nexus.tables.check import (
    BLOCKING_CODES,
    CLOSED_BY_ESCAPE,
    COVERAGE_GAP,
    OVERLAP,
    UNMATCHED_ASSIGNMENT,
    UNPROVABLE_COVERAGE,
    Finding,
    check_table,
    exit_code,
)
from nexus.tables.load import (
    TableLoadError,
    Dimension,
    DuplicateRowIdError,
    FrozenMapping,
    MatchKeysMismatchError,
    MultipleEscapesInGroupError,
    MultipleOutcomesError,
    Row,
    Table,
    TableLoadError,
    UndeclaredDimensionError,
    UnknownLiteralError,
    load_packaged_table,
    load_table,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "tables"


def codes_of(findings: list[Finding]) -> list[str]:
    return sorted(f.code for f in findings)


def codes_for(findings: list[Finding], **match: str) -> list[str]:
    return sorted(f.code for f in findings if f.group == match)


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.code in BLOCKING_CODES]


# --------------------------------------------------------------------------
# Fixture (a): RDR lifecycle (state-machine kind)


def test_rdr_lifecycle_clean_has_no_blocking_findings():
    table = load_table(FIXTURES / "rdr_lifecycle_clean.toml")
    findings = check_table(table)
    assert blocking(findings) == []


def test_rdr_lifecycle_clean_create_is_silent():
    """`create` guards nothing -- a state-machine zero-dim group is
    legitimate and must produce NO finding at all, not even an advisory."""
    table = load_table(FIXTURES / "rdr_lifecycle_clean.toml")
    findings = check_table(table)
    assert codes_for(findings, event="create") == []


def test_rdr_lifecycle_clean_every_guarded_event_closed_by_escape():
    """Every event but `create` is proved only via its bare escape row, so
    each earns exactly one advisory naming that row."""
    table = load_table(FIXTURES / "rdr_lifecycle_clean.toml")
    findings = check_table(table)
    guarded_events = {
        "gate-pass", "gate-block", "accept", "close",
        "supersede", "scrap", "defer", "resume",
    }
    for event in guarded_events:
        assert codes_for(findings, event=event) == [CLOSED_BY_ESCAPE]


def test_rdr_lifecycle_defect_reports_planted_overlap_and_gap():
    table = load_table(FIXTURES / "rdr_lifecycle_defect.toml")
    findings = check_table(table)

    close_findings = [f for f in findings if f.group == {"event": "close"}]
    assert OVERLAP in [f.code for f in close_findings]
    overlap = next(f for f in close_findings if f.code == OVERLAP)
    assert {overlap.detail["row_a"], overlap.detail["row_b"]} == {"close-open", "close-extra"}
    assert overlap.detail["intersection_count"] == 1  # state=accepted only

    scrap_findings = [f for f in findings if f.group == {"event": "scrap"}]
    assert codes_of(scrap_findings) == [COVERAGE_GAP]
    gap = scrap_findings[0]
    assert gap.detail["missing_count"] == 6
    assert gap.detail["product_size"] == 7

    # Non-vacuity: the defect variant must actually be red.
    assert blocking(findings) != []
    assert {OVERLAP, COVERAGE_GAP} <= {f.code for f in blocking(findings)}


def test_rdr_lifecycle_fixed_after_removing_planted_defects():
    """Fixing the defect variant (drop the extra overlap row, restore the
    escape row) must return it to exactly the clean fixture's finding set."""
    clean = load_table(FIXTURES / "rdr_lifecycle_clean.toml")
    clean_findings = check_table(clean)
    scrap_otherwise = next(r for r in clean.rows if r.id == "scrap-otherwise")

    defect = load_table(FIXTURES / "rdr_lifecycle_defect.toml")
    fixed_rows = tuple(r for r in defect.rows if r.id != "close-extra") + (scrap_otherwise,)
    fixed = dataclasses.replace(defect, rows=fixed_rows)
    fixed_findings = check_table(fixed)

    assert blocking(fixed_findings) == []
    assert sorted(f.code for f in fixed_findings) == sorted(f.code for f in clean_findings)


# --------------------------------------------------------------------------
# Fixture (b): release-floor decision table (decision-table kind)


def test_release_decision_clean_has_no_findings_at_all():
    """Fully enumerated, no escape row: proved with nothing to report. This
    fixture is a true disjoint partition -- every row's accepted set is
    pairwise disjoint from every other row's, zero subsumption included --
    so it stays at zero findings under the no-hit-policy overlap rule
    (round-2 critique, T2 nexus/critique-nexus-j9z30-1-round2-2026-09-01
    [24008], which verified this fixture sums to exactly the full 36-cell
    product with zero pairwise intersection)."""
    table = load_table(FIXTURES / "release_decision_clean.toml")
    findings = check_table(table)
    assert findings == []


def test_release_decision_defect_reports_planted_overlap_and_gap():
    """Under RDR-201's no-hit-policy commitment (sec Background: there is
    no hit policy, so an overlap is a lint failure rather than something
    a priority order resolves), ANY non-empty intersection among
    participants is an overlap -- including strict subsumption. This
    fixture's own "at-or-above-floor" row (guard={cloud_vs_floor:
    [equal,above]}, unconstrained mode/ledger) is a strict SUPERSET of both
    planted rows (overlap-x-bare-or-paired, overlap-y-paired-or-auto), so
    it participates in two more overlap pairs beyond the originally-planted
    x-vs-y pair -- a real three-way ambiguity (round-2 critique, T2
    nexus/critique-nexus-j9z30-1-round2-2026-09-01 [24008]): the cell
    (cloud_vs_floor=equal, mode=paired, ledger=all-additive) is accepted by
    all three rows, two of which emit conflicting verdicts."""
    table = load_table(FIXTURES / "release_decision_defect.toml")
    findings = check_table(table)
    decide_findings = codes_for(findings, decision="decide")
    assert decide_findings == sorted([COVERAGE_GAP, OVERLAP, OVERLAP, OVERLAP])

    overlaps = [f for f in findings if f.code == OVERLAP]
    assert len(overlaps) == 3
    pairs = {frozenset((f.detail["row_a"], f.detail["row_b"])) for f in overlaps}
    assert pairs == {
        frozenset({"at-or-above-floor", "overlap-x-bare-or-paired"}),
        frozenset({"at-or-above-floor", "overlap-y-paired-or-auto"}),
        frozenset({"overlap-x-bare-or-paired", "overlap-y-paired-or-auto"}),
    }
    intersection_by_pair = {
        frozenset((f.detail["row_a"], f.detail["row_b"])): f.detail["intersection_count"] for f in overlaps
    }
    assert intersection_by_pair[frozenset({"at-or-above-floor", "overlap-x-bare-or-paired"})] == 6
    assert intersection_by_pair[frozenset({"at-or-above-floor", "overlap-y-paired-or-auto"})] == 6
    assert intersection_by_pair[frozenset({"overlap-x-bare-or-paired", "overlap-y-paired-or-auto"})] == 3

    gap = next(f for f in findings if f.code == COVERAGE_GAP)
    assert gap.detail["missing_count"] == 2
    assert gap.detail["product_size"] == 36
    missing_cells = {tuple(sorted(d.items())) for d in gap.detail["missing_sample"]}
    assert missing_cells == {
        tuple(sorted({"cloud_vs_floor": "below", "mode": "paired-auto", "ledger": "non-additive"}.items())),
        tuple(sorted({"cloud_vs_floor": "below", "mode": "paired-auto", "ledger": "none"}.items())),
    }


def test_release_decision_fixed_after_removing_planted_defects():
    defect = load_table(FIXTURES / "release_decision_defect.toml")
    kept = tuple(
        r for r in defect.rows
        if r.id not in ("overlap-x-bare-or-paired", "overlap-y-paired-or-auto")
    )
    restored = kept + (
        Row(
            id="below-paired-auto-non-additive",
            match={"decision": "decide"},
            guard={
                "cloud_vs_floor": ("below",),
                "mode": ("paired-auto",),
                "ledger": ("non-additive", "none"),
            },
            outcome_kind="emit",
            outcome={"verdict": "block"},
            escape=False,
        ),
    )
    fixed = dataclasses.replace(defect, rows=restored)
    findings = check_table(fixed)
    assert findings == []


# --------------------------------------------------------------------------
# Capability tests: refusal to claim coverage over an undeclared or
# non-enum dimension, and the state-machine/decision-table zero-dim split.


def test_refuses_undeclared_guard_dimension_at_load():
    """RDR-201 P1.2 code review (T2 nexus/code-review-nexus-j9z30-2-2026-09-01):
    an undeclared guard key is now refused at LOAD time
    (UndeclaredDimensionError), not left for check_table to notice --
    undeclared_dimension.toml's `region` guard key has no [dimensions.region]
    section at all. Repurposes the fixture that used to document the OLD
    check-time-only behavior; test_check_table_still_flags_undeclared_dimension_on_hand_built_table
    below preserves direct unit coverage of check.py's own
    dimension_reason("undeclared-dimension") branch for a Table built
    without going through the loader."""
    with pytest.raises(UndeclaredDimensionError, match="region"):
        load_table(FIXTURES / "undeclared_dimension.toml")


def test_refuses_undeclared_match_key_dimension_at_load():
    """Same refusal, but for a MATCH key rather than a guard key --
    undeclared_match_dimension.toml's `status` match key has no
    [dimensions.status] section. This is also what makes
    _check_match_totality's match-key product well-defined: every match
    key on a table that loaded successfully is guaranteed declared."""
    with pytest.raises(UndeclaredDimensionError, match="status"):
        load_table(FIXTURES / "undeclared_match_dimension.toml")


def test_check_table_still_flags_undeclared_dimension_on_hand_built_table():
    """check.py's own dimension_reason("undeclared-dimension") branch is
    now unreachable via the normal loader path (UndeclaredDimensionError
    refuses it first), but stays live defense-in-depth for a Table
    constructed directly, bypassing load_table -- prove it still fires."""
    table = Table(
        id="hand-built-undeclared",
        kind="decision-table",
        dimensions={"decision": Dimension(name="decision", domain=("decide",))},
        match_keys=("decision",),
        rows=(
            Row(
                id="r1",
                match={"decision": "decide"},
                guard={"region": ("eu",)},  # "region" never declared
                outcome_kind="emit",
                outcome={"verdict": "ok"},
                escape=False,
            ),
        ),
    )
    findings = check_table(table)
    unprovable = [f for f in findings if f.code == UNPROVABLE_COVERAGE]
    assert any(f.detail.get("dimension") == "region" for f in unprovable)
    assert any(f.detail.get("reason") == "undeclared-dimension" for f in unprovable)
    assert COVERAGE_GAP not in {f.code for f in findings}


def test_refuses_non_enum_dimension():
    table = load_table(FIXTURES / "non_enum_dimension.toml")
    findings = check_table(table)
    unprovable = [f for f in findings if f.code == UNPROVABLE_COVERAGE]
    assert any(
        f.detail.get("dimension") == "retries" and f.detail.get("reason") == "non-enum-dimension"
        for f in unprovable
    )


def test_refuses_dimension_with_no_declared_domain():
    """A dimension's kind is enum but its domain is empty: nothing to
    project, so coverage over it is unprovable at check time (loading
    succeeds -- the literal is unchecked when the domain is empty)."""
    table = load_table(FIXTURES / "empty_domain_dimension.toml")
    findings = check_table(table)
    assert len(findings) == 1
    assert findings[0].code == UNPROVABLE_COVERAGE
    assert findings[0].detail["reason"] == "dimension-not-finite"
    assert findings[0].detail["dimension"] == "tier"


def test_decision_table_zero_participating_dimension_is_blocking():
    table = load_table(FIXTURES / "no_participating_dimension_decision_table.toml")
    findings = check_table(table)
    assert len(findings) == 1
    assert findings[0].code == UNPROVABLE_COVERAGE
    assert findings[0].detail["reason"] == "no-participating-dimension"


def test_state_machine_zero_participating_dimension_is_silent():
    """Silent on COVERAGE (no blocking finding); the declared-but-unused
    dimension itself is now reported as the unused-dimension advisory."""
    table = load_table(FIXTURES / "no_participating_dimension_state_machine.toml")
    findings = check_table(table)
    assert [f.code for f in findings] == [check_mod.UNUSED_DIMENSION]
    assert check_mod.exit_code(findings) == 0


# --------------------------------------------------------------------------
# Load-time refusals


def test_refuses_out_of_domain_literal():
    with pytest.raises(UnknownLiteralError, match="not in declared domain"):
        load_table(FIXTURES / "out_of_domain_literal.toml")


def test_refuses_bad_table_kind():
    with pytest.raises(TableLoadError, match="table.kind"):
        load_table(FIXTURES / "bad_table_kind.toml")


def test_refuses_duplicate_row_id():
    with pytest.raises(DuplicateRowIdError, match="duplicate row id"):
        load_table(FIXTURES / "duplicate_row_id.toml")


def test_in_atom_on_match_expands_one_row_per_member():
    table = load_table(FIXTURES / "match_expansion.toml")
    assert {r.match["event"] for r in table.rows} == {"x", "y"}
    assert len(table.rows) == 4
    assert {r.id for r in table.rows} == {"r1#x", "r1#y", "r1-rest#x", "r1-rest#y"}
    findings = check_table(table)
    assert blocking(findings) == []


# --------------------------------------------------------------------------
# NEW load-time refusals the production schema adds over the prototype


def test_match_keys_mismatch_refused_at_load():
    with pytest.raises(MatchKeysMismatchError):
        load_table(FIXTURES / "match_keys_mismatch.toml")


def test_multiple_outcomes_refused_at_load():
    with pytest.raises(MultipleOutcomesError):
        load_table(FIXTURES / "multiple_outcomes.toml")


def test_multiple_escapes_in_group_refused_at_load():
    with pytest.raises(MultipleEscapesInGroupError):
        load_table(FIXTURES / "multiple_escapes_in_group.toml")


# --------------------------------------------------------------------------
# Non-vacuity (nexus-moht0 doctrine): a planted overlap and a planted gap
# must both be REPORTED by live detection code -- neutering the detector
# must make both go red (the finding disappears), proving the assertions
# above in test_rdr_lifecycle_defect_reports_planted_overlap_and_gap are
# not accidentally passing regardless of implementation.


def test_non_vacuity_neutering_detectors_removes_planted_findings(monkeypatch):
    table = load_table(FIXTURES / "rdr_lifecycle_defect.toml")

    # Sanity: with the real detectors, both planted defects are reported.
    live_findings = check_table(table)
    assert OVERLAP in {f.code for f in live_findings}
    assert COVERAGE_GAP in {f.code for f in live_findings}

    monkeypatch.setattr(check_mod, "_check_overlap", lambda group, dims, dimensions, impossible=(): [])
    monkeypatch.setattr(check_mod, "_check_coverage", lambda group, dims, dimensions, impossible=(): [])
    neutered_findings = check_table(table)

    assert OVERLAP not in {f.code for f in neutered_findings}
    assert COVERAGE_GAP not in {f.code for f in neutered_findings}


# --------------------------------------------------------------------------
# exit_code


def test_exit_code_zero_when_no_blocking_findings():
    table = load_table(FIXTURES / "rdr_lifecycle_clean.toml")
    assert exit_code(check_table(table)) == 0


def test_exit_code_one_when_blocking_finding_present():
    table = load_table(FIXTURES / "rdr_lifecycle_defect.toml")
    assert exit_code(check_table(table)) == 1


def test_table_is_frozen_dataclass():
    """Table is an immutable value object -- resolve.py (RDR-201 P1.2)
    depends on being able to dataclasses.replace() it."""
    table = load_table(FIXTURES / "rdr_lifecycle_clean.toml")
    assert isinstance(table, Table)
    with pytest.raises(dataclasses.FrozenInstanceError):
        table.id = "mutated"  # type: ignore[misc]


def test_row_and_finding_are_actually_hashable():
    """RDR-201 P1.2's resolve() wants to hash rows freely -- this must call
    hash(), not merely assert FrozenInstanceError (review finding: an
    earlier version of this test claimed hashability in its docstring
    without ever calling hash(), and Row.match/guard/Finding.group were
    plain dicts at the time, so the claim was false)."""
    table = load_table(FIXTURES / "rdr_lifecycle_clean.toml")
    row = next(r for r in table.rows if r.id == "create")
    assert hash(row) == hash(row)  # does not raise
    assert len({row, row}) == 1  # usable as a set member / dict key

    # A Row constructed directly (not via the loader) is hashable too --
    # __post_init__ coerces match/guard regardless of what the caller passes.
    direct = Row(
        id="direct",
        match={"event": "create"},
        guard={},
        outcome_kind="to",
        outcome={"state": "draft"},
        escape=False,
    )
    assert isinstance(direct.match, FrozenMapping)
    assert hash(direct) == hash(direct)
    assert direct.match == {"event": "create"}  # still dict-comparable

    findings = check_table(table)
    assert findings, "expected at least one advisory finding to hash"
    for finding in findings:
        assert hash(finding) == hash(finding)  # does not raise
        assert isinstance(finding.group, FrozenMapping)


# --------------------------------------------------------------------------
# CRITICAL 1 (nexus-akmum): overlap must be checked even when a group has
# zero guard dimensions -- the empty product still has exactly one
# assignment, and two rows accepting it is a real overlap.


def test_overlap_detected_when_group_has_no_guard_dimensions():
    table = load_table(FIXTURES / "overlap_no_guard.toml")
    findings = check_table(table)
    overlap_findings = [f for f in findings if f.code == OVERLAP]
    assert len(overlap_findings) == 1
    overlap = overlap_findings[0]
    assert {overlap.detail["row_a"], overlap.detail["row_b"]} == {"accept-a", "accept-b"}
    assert overlap.detail["intersection_count"] == 1  # the single empty-tuple assignment
    assert exit_code(findings) == 1


def test_zero_dim_group_closed_only_by_bare_escape_gets_advisory():
    """RDR-201 Sec Technical Design's no-bare-green principle (code review,
    T2 nexus/code-review-nexus-j9z30-6-2026-09-02 [24038]): a zero-guard-
    dimension group whose ONLY row accepting the single (empty-tuple)
    assignment is a bare escape row must get CLOSED_BY_ESCAPE, exactly as
    _check_coverage already does for guarded groups. Mirrors the real
    packaged rdr-lifecycle.toml's own `*-otherwise` shape: a list-valued
    match key expands into one bare-escape row per remaining status, each
    alone in its own zero-dim group."""
    table = load_table(FIXTURES / "zero_dim_closed_by_escape.toml")
    findings = check_table(table)

    escape_group_findings = [f for f in findings if f.group == {"status": "accepted", "event": "accept"}]
    assert codes_of(escape_group_findings) == [CLOSED_BY_ESCAPE]
    assert escape_group_findings[0].detail["escape_row"] == "accept-otherwise#accepted"

    # Regression: a zero-dim group proved by an ORDINARY row alone stays
    # silent -- the fix must not turn every zero-dim group into an advisory.
    draft_group_findings = [f for f in findings if f.group == {"status": "draft", "event": "accept"}]
    assert draft_group_findings == []

    assert exit_code(findings) == 0  # advisory only, never blocking


# --------------------------------------------------------------------------
# CRITICAL 2 (nexus-akmum): a non-bare escape row (one with a guard) must
# participate in overlap detection like any ordinary row; only a BARE
# escape (no guard at all) is exempt. Per RDR-201 sec Background's
# no-hit-policy commitment there is no carve-out for STRICT subsumption
# either -- any non-empty intersection among participants is an overlap
# (round-2 critique, T2 nexus/critique-nexus-j9z30-1-round2-2026-09-01
# [24008]).


def test_non_bare_escape_overlapping_ordinary_row_is_flagged():
    table = load_table(FIXTURES / "escape_overlap.toml")
    findings = check_table(table)

    overlap_group_findings = [f for f in findings if f.group == {"event": "overlap-case"}]
    overlaps = [f for f in overlap_group_findings if f.code == OVERLAP]
    assert len(overlaps) == 1
    overlap = overlaps[0]
    assert {overlap.detail["row_a"], overlap.detail["row_b"]} == {"overlap-ordinary", "overlap-escape"}
    assert overlap.detail["intersection_count"] == 1  # status=draft only
    # A non-bare escape that overlaps is not also credited with closing
    # coverage cleanly.
    assert CLOSED_BY_ESCAPE not in {f.code for f in overlap_group_findings}


def test_non_bare_escape_strict_superset_of_ordinary_row_is_flagged():
    """The round-2 critique's own repro: an escape row's guard is a STRICT
    SUPERSET of an ordinary row's guard (not an exact duplicate) -- the
    more natural authoring mistake, a rescue guard written too broadly.
    This must ALSO be flagged as overlap; there is no "layered precedence"
    exemption in RDR-201's text."""
    table = load_table(FIXTURES / "escape_overlap.toml")
    findings = check_table(table)

    superset_group_findings = [f for f in findings if f.group == {"event": "superset-case"}]
    overlaps = [f for f in superset_group_findings if f.code == OVERLAP]
    assert len(overlaps) == 1
    overlap = overlaps[0]
    assert {overlap.detail["row_a"], overlap.detail["row_b"]} == {"superset-ordinary", "superset-escape"}
    assert overlap.detail["intersection_count"] == 1  # status=draft, the ordinary row's whole set
    assert CLOSED_BY_ESCAPE not in {f.code for f in superset_group_findings}


def test_non_bare_escape_disjoint_from_ordinary_row_is_clean():
    """The other direction: a non-bare escape whose guard is DISJOINT from
    the ordinary row's guard, and which together close the full domain,
    produces NO finding at all -- proving the overlap fix does not
    over-flag a legitimate non-bare-escape rescue, and that closed-by-escape
    correctly stays reserved for a BARE escape row."""
    table = load_table(FIXTURES / "escape_overlap.toml")
    findings = check_table(table)
    clean_group_findings = [f for f in findings if f.group == {"event": "clean-case"}]
    assert clean_group_findings == []


# --------------------------------------------------------------------------
# load_packaged_table (review IMPORTANT (a)): happy path + missing-resource
# error path, using a small importable fixture package.


def test_load_packaged_table_happy_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(FIXTURES))
    table = load_packaged_table("sample_table.toml", package="_pkg_fixture")
    assert table.id == "packaged-sample"
    assert table.kind == "state-machine"
    assert len(table.rows) == 1
    assert table.rows[0].id == "create"
    assert check_table(table) == []


def test_load_packaged_table_missing_resource_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(FIXTURES))
    with pytest.raises(FileNotFoundError):
        load_packaged_table("does-not-exist.toml", package="_pkg_fixture")


# --------------------------------------------------------------------------
# Multi-key match expansion (review Suggestion): pin the Cartesian-product
# semantics and the comma-joined suffix format when more than one match key
# is list-valued on a single row.


def test_multi_key_match_expansion_is_cartesian_product():
    table = load_table(FIXTURES / "multi_key_match_expansion.toml")
    assert len(table.rows) == 4
    ids = {r.id for r in table.rows}
    assert ids == {
        "r1#accept,draft",
        "r1#accept,accepted",
        "r1#supersede,draft",
        "r1#supersede,accepted",
    }
    matches = {(r.match["event"], r.match["status"]) for r in table.rows}
    assert matches == {
        ("accept", "draft"),
        ("accept", "accepted"),
        ("supersede", "draft"),
        ("supersede", "accepted"),
    }
    findings = check_table(table)
    assert blocking(findings) == []


# --------------------------------------------------------------------------
# unmatched-assignment (RDR-201 P1.2 critique, T2
# nexus/critique-nexus-j9z30-2-2026-09-01 [24018]): a match-key-dimension
# combination that no row names at all has no group -- prove check_table
# now reports it as a blocking finding, closing the gap where a
# checker-clean table was not actually guaranteed total.


def test_unmatched_assignment_reported_for_unnamed_status():
    table = load_table(FIXTURES / "unmatched_assignment.toml")
    findings = check_table(table)
    unmatched = [f for f in findings if f.code == UNMATCHED_ASSIGNMENT]
    assert len(unmatched) == 1
    assert unmatched[0].group == {"event": "go", "status": "c"}
    assert exit_code(findings) == 1
    assert UNMATCHED_ASSIGNMENT in BLOCKING_CODES


def test_packaged_rdr_lifecycle_table_is_clean_including_match_totality():
    """Non-vacuity anchor for the fix: the REAL packaged lifecycle table
    (not a fixture copy) must still lint clean end to end -- both the
    pre-existing per-group coverage/overlap proof and the new
    match-key-product totality proof."""
    table = load_packaged_table("rdr-lifecycle.toml")
    findings = check_table(table)
    assert UNMATCHED_ASSIGNMENT not in {f.code for f in findings}
    assert exit_code(findings) == 0


def test_declared_but_never_named_dimension_is_an_advisory():
    """RDR-201 Phase 1 critique (T2 nexus/critique-rdr-201-phase-1-2026-09-01):
    a dimension declared under [dimensions] that no row's match or guard
    names got zero signal, indistinguishable from "proved". It is now the
    unused-dimension advisory: reported, non-blocking."""
    table = load_table(FIXTURES / "unused_dimension.toml")
    findings = check_mod.check_table(table)
    unused = [f for f in findings if f.code == check_mod.UNUSED_DIMENSION]
    assert [f.detail["dimension"] for f in unused] == ["region"]
    assert check_mod.exit_code(findings) == 0
    assert check_mod.UNUSED_DIMENSION not in check_mod.BLOCKING_CODES


def test_packaged_lifecycle_table_has_no_unused_dimension():
    findings = check_mod.check_table(load_packaged_table("rdr-lifecycle.toml"))
    assert not [f for f in findings if f.code == check_mod.UNUSED_DIMENSION]


# --------------------------------------------------------------------------
# [[impossible]] guard pairs (nexus-q9u2n)

_IMPOSSIBLE_BASE = """
[table]
id = "t"
kind = "decision-table"

[dimensions.fn]
domain = ["f"]
[dimensions."fn.gate"]
domain = ["blocks", "passes"]
[dimensions."fn.probe"]
domain = ["n/a", "ok", "bad"]

[[row]]
id = "blocks"
match = { fn = "f" }
guard = { "fn.gate" = "blocks" }
emit = { exit_code = "2", message_key = "blocks" }

[[row]]
id = "ok"
match = { fn = "f" }
guard = { "fn.gate" = "passes", "fn.probe" = "ok" }
emit = { exit_code = "0", message_key = "ok" }

[[row]]
id = "bad"
match = { fn = "f" }
guard = { "fn.gate" = "passes", "fn.probe" = "bad" }
emit = { exit_code = "1", message_key = "bad" }
"""

_IMPOSSIBLE_PAIR = """
[[impossible]]
"fn.gate" = "passes"
"fn.probe" = "n/a"
"""


def _write(tmp_path, text: str):
    p = tmp_path / "t.toml"
    p.write_text(text)
    return p


def test_a_phantom_cell_is_a_gap_without_the_impossible_block(tmp_path):
    """(passes, n/a) is in the product and no row covers it."""
    findings = check_table(load_table(_write(tmp_path, _IMPOSSIBLE_BASE)))
    gaps = [f for f in findings if f.code == COVERAGE_GAP]
    assert len(gaps) == 1 and gaps[0].detail["missing_sample"] == [{"fn.gate": "passes", "fn.probe": "n/a"}]


def test_the_impossible_block_subtracts_the_cell_and_the_table_proves(tmp_path):
    findings = check_table(load_table(_write(tmp_path, _IMPOSSIBLE_BASE + _IMPOSSIBLE_PAIR)))
    assert findings == [], [f.to_json() for f in findings]


def test_a_row_covering_only_impossible_cells_is_a_dead_row_advisory(tmp_path):
    dead = """
[[row]]
id = "never"
match = { fn = "f" }
guard = { "fn.gate" = "passes", "fn.probe" = "n/a" }
emit = { exit_code = "0", message_key = "never" }
"""
    findings = check_table(load_table(_write(tmp_path, _IMPOSSIBLE_BASE + dead + _IMPOSSIBLE_PAIR)))
    assert [f.code for f in findings] == [check_mod.DEAD_ROW]
    assert findings[0].detail["row"] == "never"
    assert exit_code(findings) == 0, "advisory, not blocking"


def test_overlap_confined_to_an_impossible_cell_is_not_an_overlap(tmp_path):
    """Two rows that share only a ruled-out cell do not conflict."""
    wide = """
[[row]]
id = "any-probe-passes"
match = { fn = "f" }
guard = { "fn.gate" = "passes", "fn.probe" = ["n/a", "ok"] }
emit = { exit_code = "0", message_key = "wide" }
"""
    text = _IMPOSSIBLE_BASE.replace('guard = { "fn.gate" = "passes", "fn.probe" = "ok" }', 'guard = { "fn.gate" = "passes", "fn.probe" = "n/a" }')
    findings = check_table(load_table(_write(tmp_path, text + wide + _IMPOSSIBLE_PAIR)))
    assert OVERLAP not in {f.code for f in findings}, [f.to_json() for f in findings]


@pytest.mark.parametrize(
    "block, err",
    [
        ('[[impossible]]\n"fn.gate" = "passes"\n', "exactly two"),
        ('[[impossible]]\n"fn.gate" = "passes"\n"fn.nope" = "x"\n', "undeclared"),
        ('[[impossible]]\n"fn.gate" = "passes"\n"fn.probe" = "zzz"\n', "domain"),
    ],
)
def test_malformed_impossible_blocks_are_refused_at_load(tmp_path, block, err):
    with pytest.raises(TableLoadError, match=err):
        load_table(_write(tmp_path, _IMPOSSIBLE_BASE + block))
