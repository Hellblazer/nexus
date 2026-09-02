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
    UNPROVABLE_COVERAGE,
    Finding,
    check_table,
    exit_code,
)
from nexus.tables.load import (
    DuplicateRowIdError,
    FrozenMapping,
    MatchKeysMismatchError,
    MultipleEscapesInGroupError,
    MultipleOutcomesError,
    Row,
    Table,
    TableLoadError,
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
    """Fully enumerated, no escape row: proved with nothing to report."""
    table = load_table(FIXTURES / "release_decision_clean.toml")
    findings = check_table(table)
    assert findings == []


def test_release_decision_defect_reports_planted_overlap_and_gap():
    table = load_table(FIXTURES / "release_decision_defect.toml")
    findings = check_table(table)
    assert codes_for(findings, decision="decide") == sorted([COVERAGE_GAP, OVERLAP])

    overlap = next(f for f in findings if f.code == OVERLAP)
    assert {overlap.detail["row_a"], overlap.detail["row_b"]} == {
        "overlap-x-bare-or-paired", "overlap-y-paired-or-auto",
    }
    assert overlap.detail["intersection_count"] == 3  # equal x paired x ledger(3)

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


def test_refuses_undeclared_dimension():
    table = load_table(FIXTURES / "undeclared_dimension.toml")
    findings = check_table(table)
    unprovable = [f for f in findings if f.code == UNPROVABLE_COVERAGE]
    assert any(f.detail.get("dimension") == "region" for f in unprovable)
    assert any(f.detail.get("reason") == "undeclared-dimension" for f in unprovable)
    # No coverage-gap should be claimed once a dimension is unprovable.
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
    table = load_table(FIXTURES / "no_participating_dimension_state_machine.toml")
    findings = check_table(table)
    assert findings == []


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

    monkeypatch.setattr(check_mod, "_check_overlap", lambda group, dims, dimensions: [])
    monkeypatch.setattr(check_mod, "_check_coverage", lambda group, dims, dimensions: [])
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


# --------------------------------------------------------------------------
# CRITICAL 2 (nexus-akmum): a non-bare escape row (one with a guard) must
# participate in overlap detection like any ordinary row; only a BARE
# escape (no guard at all) is exempt.


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
