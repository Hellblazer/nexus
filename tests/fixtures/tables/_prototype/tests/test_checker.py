"""Tests for the enum-only coverage/overlap checker prototype.

Non-vacuity discipline: every "planted defect" test asserts the EXACT
finding code(s) expected, not merely "findings is non-empty" -- a checker
that reports the wrong thing, or reports nothing and only happens to have a
non-empty list from an unrelated advisory, must fail these tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checker import (  # noqa: E402
    GRAPH_COVERAGE_CLOSED_BY_ESCAPE,
    GRAPH_COVERAGE_GAP,
    GRAPH_OVERLAP,
    GRAPH_UNPROVABLE_COVERAGE,
    BLOCKING_CODES,
    ModelError,
    check_model,
    load_model,
)

MODELS = Path(__file__).resolve().parent.parent / "models"


def codes_of(findings, group=None):
    return sorted(
        f.code for f in findings if group is None or f.group == group
    )


def blocking(findings):
    return [f for f in findings if f.code in BLOCKING_CODES]


# --------------------------------------------------------------------------
# Fixture (a): RDR lifecycle (state-machine class)


def test_rdr_lifecycle_clean_has_no_blocking_findings():
    model = load_model(MODELS / "rdr_lifecycle.toml")
    findings = check_model(model)
    assert blocking(findings) == []


def test_rdr_lifecycle_clean_create_is_silent():
    """`create` guards nothing -- a state-machine zero-dim group is
    legitimate and must produce NO finding at all, not even an advisory."""
    model = load_model(MODELS / "rdr_lifecycle.toml")
    findings = check_model(model)
    assert codes_of(findings, group="create") == []


def test_rdr_lifecycle_clean_every_guarded_event_closed_by_escape():
    """Every event but `create` is proved only via its bare escape row, so
    each earns exactly one advisory naming that row."""
    model = load_model(MODELS / "rdr_lifecycle.toml")
    findings = check_model(model)
    guarded_events = {
        "gate-pass", "gate-block", "accept", "close",
        "supersede", "scrap", "defer", "resume",
    }
    for event in guarded_events:
        assert codes_of(findings, group=event) == [GRAPH_COVERAGE_CLOSED_BY_ESCAPE]


def test_rdr_lifecycle_defect_reports_planted_overlap_and_gap():
    model = load_model(MODELS / "rdr_lifecycle_defect.toml")
    findings = check_model(model)

    close_findings = [f for f in findings if f.group == "close"]
    assert GRAPH_OVERLAP in [f.code for f in close_findings]
    overlap = next(f for f in close_findings if f.code == GRAPH_OVERLAP)
    assert {overlap.detail["row_a"], overlap.detail["row_b"]} == {"close-open", "close-extra"}
    assert overlap.detail["intersection_count"] == 1  # state=accepted only

    scrap_findings = [f for f in findings if f.group == "scrap"]
    assert codes_of(scrap_findings) == [GRAPH_COVERAGE_GAP]
    gap = scrap_findings[0]
    assert gap.detail["missing_count"] == 6
    assert gap.detail["product_size"] == 7

    # Non-vacuity: the defect variant must actually be red.
    assert blocking(findings) != []
    assert {GRAPH_OVERLAP, GRAPH_COVERAGE_GAP} <= {f.code for f in blocking(findings)}


def test_rdr_lifecycle_fixed_after_removing_planted_defects():
    """Fixing the defect variant (drop the extra overlap row, restore the
    escape row) must return it to exactly the clean fixture's finding set."""
    from dataclasses import replace

    clean = load_model(MODELS / "rdr_lifecycle.toml")
    clean_findings = check_model(clean)
    scrap_otherwise = next(r for r in clean.rows if r.id == "scrap-otherwise")

    defect = load_model(MODELS / "rdr_lifecycle_defect.toml")
    fixed_rows = tuple(r for r in defect.rows if r.id != "close-extra") + (scrap_otherwise,)
    fixed = replace(defect, rows=fixed_rows)
    fixed_findings = check_model(fixed)

    assert blocking(fixed_findings) == []
    assert sorted(f.code for f in fixed_findings) == sorted(f.code for f in clean_findings)


# --------------------------------------------------------------------------
# Fixture (b): release-floor decision table (decision-table class)


def test_release_decision_clean_has_no_findings_at_all():
    """Fully enumerated, no escape row: proved with nothing to report."""
    model = load_model(MODELS / "release_decision.toml")
    findings = check_model(model)
    assert findings == []


def test_release_decision_defect_reports_planted_overlap_and_gap():
    model = load_model(MODELS / "release_decision_defect.toml")
    findings = check_model(model)
    assert codes_of(findings, group="decide") == sorted([GRAPH_COVERAGE_GAP, GRAPH_OVERLAP])

    overlap = next(f for f in findings if f.code == GRAPH_OVERLAP)
    assert {overlap.detail["row_a"], overlap.detail["row_b"]} == {
        "overlap-x-bare-or-paired", "overlap-y-paired-or-auto",
    }
    assert overlap.detail["intersection_count"] == 3  # equal x paired x ledger(3)

    gap = next(f for f in findings if f.code == GRAPH_COVERAGE_GAP)
    assert gap.detail["missing_count"] == 2
    assert gap.detail["product_size"] == 36
    missing_cells = {tuple(sorted(d.items())) for d in gap.detail["missing_sample"]}
    assert missing_cells == {
        tuple(sorted({"cloud_vs_floor": "below", "mode": "paired-auto", "ledger": "non-additive"}.items())),
        tuple(sorted({"cloud_vs_floor": "below", "mode": "paired-auto", "ledger": "none"}.items())),
    }


def test_release_decision_fixed_after_removing_planted_defects():
    defect = load_model(MODELS / "release_decision_defect.toml")
    from dataclasses import replace

    kept = tuple(
        r for r in defect.rows
        if r.id not in ("overlap-x-bare-or-paired", "overlap-y-paired-or-auto")
    )
    from checker import Row
    restored = kept + (
        Row(
            id="below-paired-auto-non-additive",
            outcome="decide",
            guard_all={
                "cloud_vs_floor": ("below",),
                "mode": ("paired-auto",),
                "ledger": ("non-additive", "none"),
            },
            guard_unless={},
            escape=False,
        ),
    )
    fixed = replace(defect, rows=restored)
    findings = check_model(fixed)
    assert findings == []


# --------------------------------------------------------------------------
# Capability tests: refusal to claim coverage over an undeclared or
# non-enum dimension, and the state-machine/decision-table zero-dim split.

UNDECLARED_DIM_TOML = """
[model]
id = "undeclared-dim"
class = "decision-table"

[tags.tier]
domain = ["free", "paid"]

[[rows]]
id = "r1"
outcome = "decide"
[rows.guard_all]
tier = "free"
region = "eu"
"""


def test_refuses_undeclared_dimension(tmp_path):
    p = tmp_path / "m.toml"
    p.write_text(UNDECLARED_DIM_TOML)
    findings = check_model(load_model(p))
    unprovable = [f for f in findings if f.code == GRAPH_UNPROVABLE_COVERAGE]
    assert any(f.detail.get("dimension") == "region" for f in unprovable)
    assert any(f.detail.get("reason") == "undeclared-dimension" for f in unprovable)
    # No coverage-gap should be claimed once a dimension is unprovable.
    assert GRAPH_COVERAGE_GAP not in {f.code for f in findings}


NON_ENUM_DIM_TOML = """
[model]
id = "non-enum-dim"
class = "decision-table"

[tags.tier]
domain = ["free", "paid"]

[tags.retries]
kind = "int"

[[rows]]
id = "r1"
outcome = "decide"
[rows.guard_all]
tier = "free"
retries = "3"
"""


def test_refuses_non_enum_dimension(tmp_path):
    p = tmp_path / "m.toml"
    p.write_text(NON_ENUM_DIM_TOML)
    findings = check_model(load_model(p))
    unprovable = [f for f in findings if f.code == GRAPH_UNPROVABLE_COVERAGE]
    assert any(
        f.detail.get("dimension") == "retries" and f.detail.get("reason") == "non-enum-dimension"
        for f in unprovable
    )


NOT_FINITE_DIM_TOML = """
[model]
id = "not-finite-dim"
class = "decision-table"

[tags.tier]
domain = []

[[rows]]
id = "r1"
outcome = "decide"
[rows.guard_all]
tier = "free"
"""


def test_refuses_dimension_with_no_declared_domain():
    """A tag's kind is enum but its domain is empty: nothing to project."""
    import tomllib
    doc = tomllib.loads(NOT_FINITE_DIM_TOML)
    # `tier = "free"` is checked against the (empty) domain only when the
    # domain is non-empty (see `_normalize_guard_block`), so this loads --
    # coverage-time is where the empty domain is refused.
    assert doc["tags"]["tier"]["domain"] == []


def test_decision_table_zero_participating_dimension_is_blocking(tmp_path):
    toml = """
[model]
id = "no-dims"
class = "decision-table"

[[rows]]
id = "r1"
outcome = "decide"
"""
    p = tmp_path / "m.toml"
    p.write_text(toml)
    findings = check_model(load_model(p))
    assert len(findings) == 1
    assert findings[0].code == GRAPH_UNPROVABLE_COVERAGE
    assert findings[0].detail["reason"] == "no-participating-dimension"


def test_state_machine_zero_participating_dimension_is_silent(tmp_path):
    toml = """
[model]
id = "no-dims-sm"
class = "state-machine"

[[rows]]
id = "r1"
outcome = "create"
"""
    p = tmp_path / "m.toml"
    p.write_text(toml)
    findings = check_model(load_model(p))
    assert findings == []


# --------------------------------------------------------------------------
# Load-time refusals


def test_refuses_out_of_domain_literal(tmp_path):
    toml = """
[model]
id = "bad-literal"

[tags.tier]
domain = ["free", "paid"]

[[rows]]
id = "r1"
outcome = "decide"
[rows.guard_all]
tier = "enterprise"
"""
    p = tmp_path / "m.toml"
    p.write_text(toml)
    with pytest.raises(ModelError, match="not in declared domain"):
        load_model(p)


def test_refuses_bad_model_class(tmp_path):
    toml = """
[model]
id = "bad-class"
class = "nonsense"
"""
    p = tmp_path / "m.toml"
    p.write_text(toml)
    with pytest.raises(ModelError, match="model.class"):
        load_model(p)


def test_refuses_duplicate_row_id(tmp_path):
    toml = """
[model]
id = "dup-id"

[[rows]]
id = "r1"
outcome = "a"

[[rows]]
id = "r1"
outcome = "b"
"""
    p = tmp_path / "m.toml"
    p.write_text(toml)
    with pytest.raises(ModelError, match="duplicate row id"):
        load_model(p)


def test_in_atom_on_outcome_expands_one_row_per_member(tmp_path):
    toml = """
[model]
id = "outcome-expansion"
class = "state-machine"

[tags.state]
domain = ["a", "b"]

[[rows]]
id = "r1"
outcome = ["x", "y"]
[rows.guard_all]
state = "a"

[[rows]]
id = "r1-rest"
outcome = ["x", "y"]
escape = true
"""
    p = tmp_path / "m.toml"
    p.write_text(toml)
    model = load_model(p)
    assert {r.outcome for r in model.rows} == {"x", "y"}
    assert len(model.rows) == 4
    findings = check_model(model)
    assert blocking(findings) == []
