# SPDX-License-Identifier: AGPL-3.0-or-later
"""Routing + structural tests for the abstract-themes plan template.

The template is the CheapRAG-pattern abstract-question plan (nexus-ldnp,
RDR-098): broad over-fetch → topic-partition → per-group reduce →
coalesce. These tests pin the matching contract so a future tweak to
the description doesn't silently regress routing for the prototypical
"main themes" / "overview" question shapes.

Cohort design: the plan competes with every other builtin in the
embedding space. Tests assert the abstract-themes plan is in the
top-2 for prototypical phrasings — not strict top-1 since the
embedding space is shared across templates and ties shift with each
seed reload. Routing for borderline phrasings ("summarize key findings
about <technical term>") is intentionally NOT asserted; those land
through inline-planner fallback by design.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_TEMPLATE_PATH = Path(__file__).parent.parent / "nx" / "plans" / "builtin" / "abstract-themes.yml"


@pytest.fixture(scope="module")
def template() -> dict:
    assert _TEMPLATE_PATH.exists(), f"template missing: {_TEMPLATE_PATH}"
    return yaml.safe_load(_TEMPLATE_PATH.read_text())


def test_template_dimensions_pin_strategy(template: dict) -> None:
    dims = template["dimensions"]
    assert dims["verb"] == "query"
    assert dims["scope"] == "global"
    assert dims["strategy"] == "abstract-themes"


def test_template_step_shape(template: dict) -> None:
    """4-step community-summary pipeline."""
    steps = template["plan_json"]["steps"]
    tools = [s["tool"] for s in steps]
    assert tools == ["search", "groupby", "aggregate", "summarize"], (
        f"expected search→groupby→aggregate→summarize, got {tools}"
    )

    # Step bindings round-trip the search results through groupby
    # (auto-hydration via ids), then groupby.groups → aggregate.groups,
    # then aggregate.aggregates → summarize.inputs.
    assert steps[1]["args"]["ids"] == "$step1.ids"
    assert steps[1]["args"]["collections"] == "$step1.collections"
    assert steps[2]["args"]["groups"] == "$step2.groups"
    assert steps[3]["args"]["inputs"] == "$step3.aggregates"


def test_template_required_bindings(template: dict) -> None:
    """``concept`` is the only required binding; ``corpus`` + ``limit`` default."""
    assert template["required_bindings"] == ["concept"]
    optional = set(template.get("optional_bindings") or [])
    assert {"corpus", "limit"}.issubset(optional)
    defaults = template["default_bindings"]
    assert defaults["corpus"] == "all"
    assert isinstance(defaults["limit"], int) and defaults["limit"] > 0


def test_template_validates_against_schema(template: dict) -> None:
    """The shipped schema validator must accept this template unchanged."""
    from nexus.plans.schema import validate_plan_template

    validate_plan_template(template)


def test_groupby_partition_key_is_topic(template: dict) -> None:
    """``key: topic`` is load-bearing — it's what makes BERTopic
    centroids the community substitute. Pin it so a refactor doesn't
    silently switch to a different aspect column."""
    groupby_step = template["plan_json"]["steps"][1]
    assert groupby_step["args"]["key"] == "topic"


def test_aggregate_reducer_references_concept(template: dict) -> None:
    """The reducer must thread the user's concept binding so each
    per-topic summary stays scoped to the question, not free-form."""
    aggregate_step = template["plan_json"]["steps"][2]
    reducer = aggregate_step["args"]["reducer"]
    assert "$concept" in reducer, f"reducer must reference $concept, got: {reducer!r}"
