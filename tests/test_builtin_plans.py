# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validate every builtin plan template in conexus/plans/builtin/.

Each *.yml file must:
  * Parse as valid YAML.
  * Pass validate_plan_template (required keys, dimensions, plan_json.steps).
  * Have a non-empty 'description'.
  * Pin both 'verb' and 'scope' dimensions.
  * Not silently collide with another template (unique canonical dimensions).

This suite is the CI gate for Phase 4a / Phase 6 seed shipping.
Adding a template that breaks the schema will fail here, not at runtime.

SC-6, SC-14, SC-19.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"
_YAML_FILES = sorted(_BUILTIN_DIR.glob("*.yml")) + sorted(_BUILTIN_DIR.glob("*.yaml"))


@pytest.mark.skipif(
    not _BUILTIN_DIR.exists() or not _YAML_FILES,
    reason="conexus/plans/builtin/ dir is empty - defensive skip; expected to be populated",
)
@pytest.mark.parametrize("path", _YAML_FILES, ids=[p.name for p in _YAML_FILES])
def test_builtin_template_validates(path: Path) -> None:
    """Each builtin YAML must pass validate_plan_template without error."""
    from nexus.plans.schema import validate_plan_template

    raw = yaml.safe_load(path.read_text())
    assert isinstance(raw, dict), f"{path.name}: YAML root must be a mapping"

    # Raises PlanTemplateSchemaError on any violation.
    validate_plan_template(raw)


@pytest.mark.skipif(
    not _BUILTIN_DIR.exists() or not _YAML_FILES,
    reason="conexus/plans/builtin/ dir is empty - defensive skip; expected to be populated",
)
@pytest.mark.parametrize("path", _YAML_FILES, ids=[p.name for p in _YAML_FILES])
def test_builtin_template_required_dimensions(path: Path) -> None:
    """Each builtin template must pin 'verb' AND 'scope' dimensions."""
    raw = yaml.safe_load(path.read_text())
    dims = raw.get("dimensions") or {}
    assert dims.get("verb"), f"{path.name}: missing dimensions.verb"
    assert dims.get("scope"), f"{path.name}: missing dimensions.scope"


@pytest.mark.skipif(
    not _BUILTIN_DIR.exists() or not _YAML_FILES,
    reason="conexus/plans/builtin/ dir is empty - defensive skip; expected to be populated",
)
@pytest.mark.parametrize("path", _YAML_FILES, ids=[p.name for p in _YAML_FILES])
def test_builtin_template_verb_is_reachable_by_the_category_route(path: Path) -> None:
    """Every builtin must carry a verb some question can actually derive.

    The category route selects builtins by verb, so a template whose verb
    no classifier output reaches is unroutable by construction — it would
    sit in the library looking healthy and never be offered, which is the
    failure mode the route exists to end.

    The schema validator checks dimension KEYS only, never VALUES, and
    conexus/plans/dimensions.yml's enumeration is prose and already stale
    (it omits `query` and `lookup`, which three shipped templates use), so
    nothing else in the tree would catch a typo'd verb.
    """
    from nexus.plans.matcher import _verbs_compatible
    from nexus.plans.verb_infer import _VERB_PATTERNS

    verb = (yaml.safe_load(path.read_text()).get("dimensions") or {}).get("verb")
    derivable = [v for v, _ in _VERB_PATTERNS]
    assert any(_verbs_compatible(verb, d) for d in derivable), (
        f"{path.name}: verb {verb!r} is not reachable from any verb the "
        f"classifier can derive ({derivable}) — the template can never be "
        f"routed. Either fix the verb or teach nexus.plans.verb_infer to "
        f"emit one compatible with it."
    )


@pytest.mark.skipif(
    not _BUILTIN_DIR.exists() or not _YAML_FILES,
    reason="conexus/plans/builtin/ dir is empty - defensive skip; expected to be populated",
)
def test_builtin_templates_no_dimension_collisions() -> None:
    """No two builtin templates may have the same canonical dimensions.

    Catches the identity-collision that PlanTemplateLoader enforces at
    runtime before it reaches the database UNIQUE index.
    """
    from nexus.plans.schema import canonical_dimensions_json

    seen: dict[str, str] = {}  # canonical_json → filename
    for path in _YAML_FILES:
        raw = yaml.safe_load(path.read_text())
        dims = raw.get("dimensions")
        if not isinstance(dims, dict):
            continue
        canonical = canonical_dimensions_json(dims)
        assert canonical not in seen, (
            f"Dimension collision between {path.name!r} and {seen[canonical]!r}: "
            f"both map to {canonical}"
        )
        seen[canonical] = path.name


@pytest.mark.skipif(
    not _BUILTIN_DIR.exists() or not _YAML_FILES,
    reason="conexus/plans/builtin/ dir is empty - defensive skip; expected to be populated",
)
def test_builtin_templates_load_into_library() -> None:
    """All builtin templates must load into a fresh plan library with no errors.

    Verifies the full seed-loader path including idempotency (a second
    run must produce zero inserts and zero errors).

    Ported (nexus-i711w Stage 2 sub-stage A3): the SQLite PlanLibrary is
    deleted; HttpPlanLibrary on the suite's hermetic engine substrate
    (per-test tenant => fresh library) is the only plan library. The
    ``_add_plan_dimensional_identity`` migration call went with it — the
    RDR-078 dimensional-identity schema ships in the engine's Liquibase
    changelog (plans-001-baseline.xml).
    """
    from nexus.db.t2.http_plan_library import HttpPlanLibrary
    from nexus.plans.seed_loader import load_seed_directory

    lib = HttpPlanLibrary()

    result = load_seed_directory(_BUILTIN_DIR, library=lib)

    assert result.errors == [], (
        f"Seed loader reported errors:\n" +
        "\n".join(f"  {src}: {msg}" for src, msg in result.errors)
    )
    assert result.inserted, "Expected at least one template to be inserted"
    first_run_count = len(result.inserted)

    # Idempotency: second run must skip all, insert none.
    result2 = load_seed_directory(_BUILTIN_DIR, library=lib)
    assert result2.errors == [], "Second run must be error-free"
    assert result2.inserted == [], (
        f"Second run must skip all existing templates (idempotent), "
        f"got new inserts: {result2.inserted}"
    )
    assert len(result2.skipped_existing) == first_run_count


@pytest.mark.skipif(
    not _BUILTIN_DIR.exists() or not _YAML_FILES,
    reason="conexus/plans/builtin/ dir is empty - defensive skip; expected to be populated",
)
@pytest.mark.parametrize("path", _YAML_FILES, ids=[p.name for p in _YAML_FILES])
def test_builtin_template_is_offerable_to_nx_answer(path: Path) -> None:
    """Every shipped template must be reachable by SOME question.

    Directive (Sam, 2026-08-22): we should not have unofferable plans. A
    plan requiring a typed value nothing can supply is dead weight that
    still competes for cosine rank against plans that work — and it fails
    silently, because "never offered" looks exactly like "never the best
    match".

    A typed binding is satisfiable when the plan defaults it, or when
    nx_answer can derive it from a question (`nexus.plans.binding_infer`
    — content_type from "which RDRs / papers / code", author from
    "by Grossberg"). Anything else is unreachable: three templates were
    in that state and two were fixed by adding derivation for exactly the
    values their own question shapes carry; the third
    (traverse-then-generate, requiring catalog tumbler ids) was retired,
    because no question can carry those.

    If this fails on a new template, the choice is: default the binding,
    teach binding_infer to derive it, or do not ship the template.
    """
    import json

    from nexus.plans.binding_infer import infer_typed_bindings
    from nexus.plans.schema import unsatisfiable_typed_binding

    template = yaml.safe_load(path.read_text())
    plan_json = dict(template["plan_json"])
    required = list(template.get("required_bindings") or [])
    plan_json["required_bindings"] = required

    # The most generous question this template could ever receive: one
    # naming every typed value binding_infer knows how to derive.
    derivable = set(infer_typed_bindings(
        "Which papers by Grossberg discuss this?"
    )) | set(infer_typed_bindings("What does the code do?"))
    available = frozenset({"intent", "_nx_scope"} | derivable)

    unmet = unsatisfiable_typed_binding(
        required=required,
        defaults=template.get("default_bindings"),
        available=available,
        plan_json=json.dumps(plan_json),
    )
    assert unmet is None, (
        f"{path.name} can never be offered to nx_answer: it requires the "
        f"typed binding {unmet!r}, which no question supplies and the "
        f"plan does not default. Default it, teach "
        f"nexus.plans.binding_infer to derive it, or do not ship the "
        f"template."
    )
