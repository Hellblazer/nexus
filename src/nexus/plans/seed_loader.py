# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Scoped YAML plan-template seed loader — RDR-078 P4b / P6.

Scans a directory of YAML plan templates, validates each against the
Phase 4a schema, dedups by canonical dimensions, and upserts into a
:class:`~nexus.db.t2.plan_library.PlanLibrary`.

This is the glue that ships the five builtin scenario templates
(``nx/plans/builtin/*.yml``) as ``scope:global`` seeds. The same
loader powers the Phase 6 multi-tier loader (``.nexus/plans/*.yml``,
``docs/rdr/<slug>/plans.yml``, umbrella repo plans).

Idempotency: a second run of the loader produces zero writes when
nothing on disk has changed. Implementation uses
:meth:`PlanLibrary.get_plan_by_dimensions` to short-circuit before
:meth:`PlanLibrary.save_plan`.

SC-6, SC-14.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nexus.db.t2.plan_library import PlanLibrary
from nexus.plans.schema import (
    PlanTemplateDuplicateError,
    PlanTemplateLoader,
    PlanTemplateSchemaError,
    canonical_dimensions_json,
    validate_plan_template,
)

__all__ = ["SeedLoadResult", "load_seed_directory"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedLoadResult:
    """Per-run summary of a seed-loader invocation."""

    inserted: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_scanned(self) -> int:
        return len(self.inserted) + len(self.skipped_existing) + len(self.errors)


def _default_project_for_scope(scope: str) -> str:
    """Map a template's ``scope`` dimension to the ``project`` column.

    ``scope:global`` templates live under the empty project (shared).
    Other scopes get their scope name so a project query can filter.
    Mirrors the Phase 6 loader convention; the UNIQUE index keys on
    ``(project, dimensions)`` so per-scope namespacing is required for
    two scopes with identical dimension maps to coexist.
    """
    if scope == "global":
        return ""
    return scope


def load_seed_directory(
    directory: Path,
    *,
    library: PlanLibrary,
    registered_dimensions: set[str] | None = None,
    outcome: str = "success",
    file_filter: Any = None,
    scope_override: str | None = None,
) -> SeedLoadResult:
    """Load every ``*.yml`` / ``*.yaml`` plan template under *directory*.

    *file_filter* is an optional predicate ``Callable[[Path], bool]``
    called for each candidate path; when supplied, only files for
    which it returns True are loaded. The scoped loader uses this to
    keep the umbrella ``_repo.yml`` out of the ``scope:project`` tier
    without duplicating the whole walk.

    Returns a :class:`SeedLoadResult` naming each template by filename
    and bucketing by outcome (inserted, skipped_existing, errors).

    Duplicates within the batch raise :class:`PlanTemplateDuplicateError`
    (the error is recorded, the loader continues). Schema errors record
    the filename + message and continue. Any plan whose canonical
    ``(project, dimensions)`` key already exists in *library* is
    skipped without re-inserting — the SC-14 idempotency contract.
    """
    result = SeedLoadResult()
    if not directory.exists():
        _log.info("seed_directory_missing", path=str(directory))
        return result

    template_loader = PlanTemplateLoader(
        registered_dimensions=registered_dimensions,
    )
    yaml_paths = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix in (".yml", ".yaml")
        and (file_filter is None or file_filter(p))
    )

    for path in yaml_paths:
        source = str(path)
        try:
            template = yaml.safe_load(path.read_text()) or {}
            if not isinstance(template, dict):
                raise PlanTemplateSchemaError(
                    f"YAML root is {type(template).__name__}, expected mapping"
                )
            # Scope normalisation happens in memory only — never write back
            # to the user's YAML file. When scope_override is set, replace
            # dimensions.scope (warning emitted by caller _load_tier).
            if scope_override is not None:
                dims = template.get("dimensions") or {}
                if dims.get("scope") != scope_override:
                    dims = dict(dims)
                    dims["scope"] = scope_override
                    template = dict(template)
                    template["dimensions"] = dims
            # template_loader.add() calls validate_plan_template first, which
            # raises the named PlanTemplateSchemaError on a missing/invalid
            # 'dimensions' key. We canonicalize AFTER validation so the
            # schema error surfaces as-is rather than getting masked by a
            # bare KeyError on template["dimensions"].
            template_loader.add(template, source=source)
            canonical = canonical_dimensions_json(template["dimensions"])
        except PlanTemplateDuplicateError as exc:
            result.errors.append((source, str(exc)))
            continue
        except PlanTemplateSchemaError as exc:
            result.errors.append((source, str(exc)))
            continue
        except Exception as exc:
            result.errors.append((source, f"{type(exc).__name__}: {exc}"))
            continue

        project = _default_project_for_scope(
            template["dimensions"].get("scope", "")
        )
        existing = library.get_plan_by_dimensions(
            project=project, dimensions=canonical,
        )
        if existing is not None:
            result.skipped_existing.append(path.name)
            continue

        dimensions = template["dimensions"]
        # Carry binding declarations into the stored plan_json so
        # ``Match.from_plan_row`` can recover them (nexus-80tk). The
        # YAML author declares required_bindings/optional_bindings at
        # the top level; without this merge they never reach the DB,
        # and ``_validate_bindings`` sees an empty list and lets
        # unfilled ``$var`` placeholders leak into operator prompts.
        plan_json_payload: dict[str, Any] = dict(template["plan_json"])
        if template.get("required_bindings"):
            plan_json_payload["required_bindings"] = list(
                template["required_bindings"]
            )
        if template.get("optional_bindings"):
            plan_json_payload["optional_bindings"] = list(
                template["optional_bindings"]
            )
        library.save_plan(
            query=template["description"],
            plan_json=json.dumps(plan_json_payload),
            outcome=outcome,
            tags=template.get("tags", "") or "",
            project=project,
            name=template.get("name"),
            verb=dimensions.get("verb"),
            scope=dimensions.get("scope"),
            dimensions=canonical,
            default_bindings=(
                json.dumps(template["default_bindings"])
                if template.get("default_bindings") else None
            ),
            parent_dims=(
                canonical_dimensions_json(template["parent"])
                if template.get("parent") else None
            ),
        )
        result.inserted.append(path.name)

    return result
