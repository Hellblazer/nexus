# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for the plan template schema validator (nexus-05i.4 / P4a).

Schema reference: RDR-078 §Phase 4a (line 295-321 of the RDR file).
"""
from __future__ import annotations

import pytest


def _full_template() -> dict:
    """The RDR §Phase 4a exemplar template — used as a green baseline."""
    return {
        "name": "default",
        "description": "Walk the link graph from a seed RDR.",
        "dimensions": {
            "verb": "research",
            "scope": "global",
            "strategy": "default",
        },
        "default_bindings": {"depth": 2},
        "required_bindings": ["intent"],
        "optional_bindings": ["limit"],
        "tags": "builtin-template,research",
        "plan_json": {
            "steps": [
                {
                    "tool": "search",
                    "args": {"query": "$intent"},
                    "scope": {"taxonomy_domain": "prose"},
                },
                {
                    "tool": "traverse",
                    "args": {"seeds": "$step1.tumblers", "depth": 2},
                    "purpose": "find-implementations",
                },
            ],
        },
    }


# ── Happy path ──────────────────────────────────────────────────────────────


def test_schema_accepts_full_template() -> None:
    from nexus.plans.schema import validate_plan_template

    validate_plan_template(_full_template())  # must not raise


def test_schema_accepts_minimal_template() -> None:
    """Required fields only: description, dimensions{verb, scope}, plan_json."""
    from nexus.plans.schema import validate_plan_template

    validate_plan_template({
        "description": "minimal valid plan",
        "dimensions": {"verb": "research", "scope": "global"},
        "plan_json": {"steps": []},
    })


# ── Required-field rejections ───────────────────────────────────────────────


def test_schema_rejects_missing_description() -> None:
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_template

    t = _full_template()
    del t["description"]
    with pytest.raises(PlanTemplateSchemaError, match="description"):
        validate_plan_template(t)


def test_schema_rejects_empty_description() -> None:
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_template

    t = _full_template()
    t["description"] = "   "
    with pytest.raises(PlanTemplateSchemaError, match="description"):
        validate_plan_template(t)


def test_schema_rejects_missing_dimensions() -> None:
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_template

    t = _full_template()
    del t["dimensions"]
    with pytest.raises(PlanTemplateSchemaError, match="dimensions"):
        validate_plan_template(t)


def test_schema_rejects_missing_required_dimension() -> None:
    """``verb`` and ``scope`` are required in the dimensions map."""
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_template

    t = _full_template()
    del t["dimensions"]["verb"]
    with pytest.raises(PlanTemplateSchemaError, match=r"verb"):
        validate_plan_template(t)


def test_schema_rejects_missing_scope_dimension() -> None:
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_template

    t = _full_template()
    del t["dimensions"]["scope"]
    with pytest.raises(PlanTemplateSchemaError, match=r"scope"):
        validate_plan_template(t)


def test_schema_rejects_missing_plan_json_steps() -> None:
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_template

    t = _full_template()
    t["plan_json"] = {}
    with pytest.raises(PlanTemplateSchemaError, match="steps"):
        validate_plan_template(t)


# ── SC-16: link_types / purpose mutual exclusion on traverse steps ─────────


def test_schema_rejects_link_types_and_purpose_together() -> None:
    """Traverse steps must declare *either* ``link_types`` or
    ``purpose`` — never both. SC-16."""
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_template

    t = _full_template()
    t["plan_json"]["steps"] = [{
        "tool": "traverse",
        "args": {"seeds": ["1.1"], "depth": 2},
        "purpose": "find-implementations",
        "link_types": ["implements"],
    }]
    with pytest.raises(PlanTemplateSchemaError, match="link_types|purpose"):
        validate_plan_template(t)


def test_schema_accepts_traverse_with_purpose_only() -> None:
    from nexus.plans.schema import validate_plan_template

    t = _full_template()
    t["plan_json"]["steps"] = [{
        "tool": "traverse",
        "args": {"seeds": ["1.1"]},
        "purpose": "find-implementations",
    }]
    validate_plan_template(t)


def test_schema_accepts_traverse_with_link_types_only() -> None:
    from nexus.plans.schema import validate_plan_template

    t = _full_template()
    t["plan_json"]["steps"] = [{
        "tool": "traverse",
        "args": {"seeds": ["1.1"]},
        "link_types": ["implements", "implements-heuristic"],
    }]
    validate_plan_template(t)


# ── SC-19: lenient unknown-dimension warning ────────────────────────────────


def test_unregistered_dimension_warns_lenient(caplog) -> None:
    """Default behaviour: unknown dimensions warn but the template
    still validates (forward-compat seam for new dimensions)."""
    import logging
    from nexus.plans.schema import validate_plan_template

    t = _full_template()
    t["dimensions"]["future_dimension"] = "experimental"
    registered = {"verb", "scope", "strategy"}
    with caplog.at_level(logging.WARNING):
        validate_plan_template(t, registered_dimensions=registered)


def test_strict_unknown_dimension_raises() -> None:
    """``strict=True`` upgrades the warning to a raise."""
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_template

    t = _full_template()
    t["dimensions"]["future_dimension"] = "experimental"
    with pytest.raises(PlanTemplateSchemaError, match="future_dimension"):
        validate_plan_template(
            t,
            registered_dimensions={"verb", "scope", "strategy"},
            strict=True,
        )


# ── SC-18: canonical-JSON dedup collision (loader-level) ───────────────────


def test_canonical_dimensions_dedup_collision_names_both_sources() -> None:
    """Two plans with the same canonical dimension map → the loader
    rejects the later one with both source labels in the error."""
    from nexus.plans.schema import (
        PlanTemplateDuplicateError,
        PlanTemplateLoader,
    )

    loader = PlanTemplateLoader()
    loader.add(_full_template(), source="nx/plans/builtin/research.yml")

    with pytest.raises(PlanTemplateDuplicateError) as excinfo:
        loader.add(_full_template(), source=".nexus/plans/research-override.yml")

    msg = str(excinfo.value)
    assert "nx/plans/builtin/research.yml" in msg
    assert ".nexus/plans/research-override.yml" in msg


def test_canonical_dimensions_collision_ignores_dimension_order() -> None:
    """Same dimensions in different declaration order → still a collision."""
    from nexus.plans.schema import (
        PlanTemplateDuplicateError,
        PlanTemplateLoader,
    )

    a = _full_template()
    b = _full_template()
    b["dimensions"] = {"scope": "global", "verb": "research", "strategy": "default"}

    loader = PlanTemplateLoader()
    loader.add(a, source="a.yml")
    with pytest.raises(PlanTemplateDuplicateError):
        loader.add(b, source="b.yml")


def test_loader_distinct_dimensions_coexist() -> None:
    from nexus.plans.schema import PlanTemplateLoader

    a = _full_template()
    b = _full_template()
    b["dimensions"]["verb"] = "review"  # different verb → distinct identity

    loader = PlanTemplateLoader()
    loader.add(a, source="a.yml")
    loader.add(b, source="b.yml")  # must not raise


# ── SC-19: dimensions registry ships with required keys ───────────────────


def test_dimensions_registry_ships_required_keys() -> None:
    """``nx/plans/dimensions.yml`` registers at least the five canonical
    dimensions used by the meta-seeds (verb, scope, strategy + 2 more
    to cover specialisation axes)."""
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[1] / "nx" / "plans" / "dimensions.yml"
    assert path.exists(), f"missing {path}"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), "dimensions.yml must be a mapping"
    keys = set(data.keys())
    for required in ("verb", "scope", "strategy"):
        assert required in keys, f"required dimension {required} missing"
    assert len(keys) >= 5, f"need ≥5 registered dimensions, found {len(keys)}: {sorted(keys)}"
