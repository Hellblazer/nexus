# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for the five RDR-078 scenario template seeds (nexus-05i.6).

Covers SC-6 (scenario seeds ship traverse; debug is intentionally flat)
and SC-14 (loader idempotency under the ``UNIQUE (project, dimensions)``
constraint).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


SEEDS_DIR = (
    Path(__file__).resolve().parents[1] / "nx" / "plans" / "builtin"
)


REQUIRED_SEEDS: tuple[tuple[str, str], ...] = (
    ("research-default.yml", "research"),
    ("review-default.yml", "review"),
    ("analyze-default.yml", "analyze"),
    ("debug-default.yml", "debug"),
    ("document-default.yml", "document"),
)


def _load_yaml(name: str) -> dict:
    return yaml.safe_load((SEEDS_DIR / name).read_text())


@pytest.fixture()
def library(tmp_path: Path):
    """Fresh PlanLibrary with the RDR-078 schema inline."""
    from nexus.db.t2.plan_library import PlanLibrary

    return PlanLibrary(tmp_path / "plans.db")


# ── Shipped seeds ──────────────────────────────────────────────────────────


def test_all_five_seeds_present() -> None:
    for filename, _ in REQUIRED_SEEDS:
        path = SEEDS_DIR / filename
        assert path.exists(), f"missing seed: {path}"


def test_all_five_seeds_validate_as_templates() -> None:
    from nexus.plans.schema import validate_plan_template

    for filename, _ in REQUIRED_SEEDS:
        template = _load_yaml(filename)
        validate_plan_template(template)  # must not raise


def test_seed_dimensions_pin_scope_global() -> None:
    for filename, expected_verb in REQUIRED_SEEDS:
        tmpl = _load_yaml(filename)
        assert tmpl["dimensions"]["scope"] == "global"
        assert tmpl["dimensions"]["verb"] == expected_verb


# ── SC-6: traverse presence / absence ──────────────────────────────────────


def test_four_seeds_use_traverse() -> None:
    """research, review, analyze, document each have ≥1 traverse step."""
    for filename in (
        "research-default.yml",
        "review-default.yml",
        "analyze-default.yml",
        "document-default.yml",
    ):
        tmpl = _load_yaml(filename)
        tools = {step["tool"] for step in tmpl["plan_json"]["steps"]}
        assert "traverse" in tools, f"{filename} missing traverse step"


def test_debug_seed_is_flat() -> None:
    """The ``verb:debug`` seed is intentionally flat: no traverse step."""
    tmpl = _load_yaml("debug-default.yml")
    tools = {step["tool"] for step in tmpl["plan_json"]["steps"]}
    assert "traverse" not in tools


# ── SC-14: loader idempotency ──────────────────────────────────────────────


_SCENARIO_FILES = {fn for fn, _ in REQUIRED_SEEDS}


def test_seed_loader_writes_all_five(library) -> None:
    """All five scenario seeds insert. Loader also picks up other
    builtin seeds (meta-seeds etc.) from the same directory; we pin
    the scenario subset explicitly."""
    from nexus.plans.seed_loader import load_seed_directory

    result = load_seed_directory(SEEDS_DIR, library=library)
    assert result.errors == [], f"unexpected errors: {result.errors}"
    inserted_scenarios = set(result.inserted) & _SCENARIO_FILES
    assert inserted_scenarios == _SCENARIO_FILES


def test_seed_loader_idempotent(library) -> None:
    from nexus.plans.seed_loader import load_seed_directory

    first = load_seed_directory(SEEDS_DIR, library=library)
    assert _SCENARIO_FILES <= set(first.inserted)

    second = load_seed_directory(SEEDS_DIR, library=library)
    assert second.inserted == []
    # Idempotency: every scenario seed skipped on the second pass.
    assert _SCENARIO_FILES <= set(second.skipped_existing)


def test_seed_loader_persists_dimensional_fields(library) -> None:
    """The loader fills verb / scope / dimensions / name on each row
    so downstream plan_match filters and the UNIQUE index work."""
    from nexus.plans.seed_loader import load_seed_directory

    load_seed_directory(SEEDS_DIR, library=library)

    rows = library.list_active_plans(outcome="success")
    by_verb = {r["verb"]: r for r in rows if r["verb"]}
    for verb in ("research", "review", "analyze", "debug", "document"):
        row = by_verb.get(verb)
        assert row is not None, f"verb:{verb} not persisted"
        assert row["scope"] == "global"
        assert row["name"] == "default"
        assert row["dimensions"], f"canonical dimensions missing for verb:{verb}"


def test_seed_loader_stores_plan_json_as_string(library) -> None:
    """The library round-trip must preserve ``plan_json`` as a string
    so :class:`Match.from_plan_row` can ``json.loads`` it."""
    from nexus.plans.match import Match
    from nexus.plans.seed_loader import load_seed_directory

    load_seed_directory(SEEDS_DIR, library=library)
    row = next(
        r for r in library.list_active_plans(outcome="success")
        if r["verb"] == "research"
    )
    match = Match.from_plan_row(row)
    plan = json.loads(match.plan_json)
    assert "steps" in plan
    assert any(s["tool"] == "traverse" for s in plan["steps"])


def test_seed_loader_malformed_yaml_skipped_not_raised(library, tmp_path) -> None:
    """A malformed YAML in the directory records an error but does
    not abort the rest of the run."""
    from nexus.plans.seed_loader import load_seed_directory

    fake = tmp_path / "seeds"
    fake.mkdir()
    (fake / "bad.yml").write_text("not: [valid")  # unterminated list
    (fake / "good.yml").write_text(
        (SEEDS_DIR / "debug-default.yml").read_text()
    )

    result = load_seed_directory(fake, library=library)
    assert len(result.inserted) == 1
    assert len(result.errors) == 1
    assert "bad.yml" in result.errors[0][0]


# ── Descriptions support plan_match retrieval ──────────────────────────────


def test_seed_descriptions_are_discriminative() -> None:
    """Descriptions are dense enough for cosine matching — each
    description ≥ 80 chars and names the verb explicitly."""
    for filename, expected_verb in REQUIRED_SEEDS:
        tmpl = _load_yaml(filename)
        description = tmpl["description"]
        assert len(description) >= 80, (
            f"{filename} description too short for plan_match "
            f"({len(description)} chars)"
        )
        # Verb should appear (in some form) so cosine has a strong anchor.
        # Allow morphological variants (research → research, review → review, etc.)
        anchors = {
            "research": ("research", "design", "architecture"),
            "review": ("review", "audit", "critique"),
            "analyze": ("analysis", "synthesis", "analyze"),
            "debug": ("debug", "failing"),
            "document": ("documentation", "documentation authoring", "document"),
        }
        found = any(
            a.lower() in description.lower() for a in anchors[expected_verb]
        )
        assert found, (
            f"{filename} description lacks verb anchor; expected one of "
            f"{anchors[expected_verb]}"
        )
