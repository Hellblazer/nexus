# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validate every builtin plan template in nx/plans/builtin/.

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

_BUILTIN_DIR = Path(__file__).parent.parent / "nx" / "plans" / "builtin"
_YAML_FILES = sorted(_BUILTIN_DIR.glob("*.yml")) + sorted(_BUILTIN_DIR.glob("*.yaml"))


@pytest.mark.skipif(
    not _BUILTIN_DIR.exists() or not _YAML_FILES,
    reason="nx/plans/builtin/ has no *.yml files yet (Phase 6 not shipped)",
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
    reason="nx/plans/builtin/ has no *.yml files yet (Phase 6 not shipped)",
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
    reason="nx/plans/builtin/ has no *.yml files yet (Phase 6 not shipped)",
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
    reason="nx/plans/builtin/ has no *.yml files yet (Phase 6 not shipped)",
)
def test_builtin_templates_load_into_library(tmp_path: Path) -> None:
    """All builtin templates must load into a fresh PlanLibrary with no errors.

    Verifies the full seed-loader path including idempotency (a second
    run must produce zero inserts and zero errors).
    """
    from nexus.db.migrations import _add_plan_dimensional_identity
    from nexus.db.t2.plan_library import PlanLibrary
    from nexus.plans.seed_loader import load_seed_directory

    lib = PlanLibrary(tmp_path / "plans.db")
    _add_plan_dimensional_identity(lib.conn)
    lib.conn.commit()

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
