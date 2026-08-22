# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Scoped YAML plan-template seed loader — RDR-078 P4b / P6.

Scans a directory of YAML plan templates, validates each against the
Phase 4a schema, dedups by canonical dimensions, and upserts into a
:class:`~nexus.db.t2.http_plan_library.HttpPlanLibrary`.

This is the glue that ships the five builtin scenario templates
(``conexus/plans/builtin/*.yml``) as ``scope:global`` seeds. The same
loader powers the Phase 6 multi-tier loader (``.nexus/plans/*.yml``,
``docs/rdr/<slug>/plans.yml``, umbrella repo plans).

Idempotency: a second run of the loader produces zero writes when
nothing on disk has changed. Implementation uses
:meth:`HttpPlanLibrary.get_plan_by_dimensions` to short-circuit before
:meth:`HttpPlanLibrary.save_plan`.

RECONCILE (nexus-f1mbo). The insert-only path above is idempotent but
also *inert*: an edited template on disk can never reach a library that
already holds a row for its dimensions, so the live library froze at
whatever the first seed run inserted (April 2026 on this project's own
install — 2 templates never seeded at all, 3 descriptions drifted).
Passing ``reconcile=True`` adds the missing update leg: a row whose
content differs from disk is rewritten in place.

Two mechanics matter and neither is obvious from the client API:

* The library's UNIQUE key is ``(tenant_id, project, dimensions)`` but
  :meth:`HttpPlanLibrary.save_plan` upserts on ``(tenant_id, project,
  query)`` — the *description text*. So a template whose description
  changed cannot be updated by calling ``save_plan`` alone: it would
  INSERT under the new description and collide with the dimensional
  unique index. The reconcile path therefore deletes the stale row
  first when (and only when) the description changed.
* ``save_plan`` resets ``created_at`` and every counter to zero, by
  design (it mirrors the pre-service Python contract). A reconciled row
  loses its match/use history. That is correct for a row whose content
  changed — the counters described different text — but it is why
  unchanged rows are still skipped rather than blindly rewritten.

Grown plans are the only plans in this library that currently match
anything, so the reconcile path refuses to touch any row tagged
``grown`` or ``ad-hoc`` even when its dimensions collide with a
template, and records the refusal instead of overwriting.

Orphans — library rows whose dimensions match no template on disk — are
LEFT IN PLACE and reported, never deleted. Disk is authoritative for the
templates it ships, not for the whole table: an orphan is as likely to
be a template from a different plugin version or tier as it is to be
dead weight, and deletion is irreversible.

SC-6, SC-14.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only (PEP 563 lazy): the runtime argument is whatever
    # the caller passes — production passes HttpPlanLibrary, the only
    # plan library left after nexus-i711w Stage 2 sub-stage A3 deleted
    # the SQLite PlanLibrary.
    from nexus.db.t2.http_plan_library import HttpPlanLibrary
from nexus.plans.schema import (
    PlanTemplateDuplicateError,
    PlanTemplateLoader,
    PlanTemplateSchemaError,
    canonical_dimensions_json,
    validate_plan_template,
)

__all__ = [
    "DesiredRow",
    "SeedLoadResult",
    "desired_row_for_template",
    "load_seed_directory",
]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedLoadResult:
    """Per-run summary of a seed-loader invocation."""

    inserted: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    #: Rows rewritten because the on-disk template no longer matched the
    #: stored row (``reconcile=True`` only; empty on the insert-only path).
    updated: list[str] = field(default_factory=list)
    #: ``(filename, reason)`` for a template whose dimensions collide with a
    #: row the reconcile path refuses to overwrite (a grown / ad-hoc plan).
    #: Reported rather than raised: one protected collision must not abort
    #: the rest of the seed run.
    protected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_scanned(self) -> int:
        return (
            len(self.inserted)
            + len(self.skipped_existing)
            + len(self.errors)
            + len(self.updated)
            + len(self.protected)
        )


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


@dataclass(frozen=True)
class DesiredRow:
    """The library row a template says should exist, before any comparison.

    Public so ``nx doctor --check-plan-library`` can run the same
    disk-vs-live comparison the loader uses, rather than a second,
    drift-prone transcription of it.
    """

    query: str
    plan_json: str
    tags: str
    name: str | None
    verb: str | None
    scope: str | None
    default_bindings: str | None
    parent_dims: str | None

    def differs_from(self, existing: dict[str, Any]) -> bool:
        """True when the stored row no longer matches this template.

        ``dimensions`` is excluded deliberately: it is the lookup key, so
        it matches by construction. ``verb`` / ``scope`` ARE compared,
        because a row can be reached by canonical dimensions while
        carrying stale denormalised copies of them.
        """
        if (existing.get("query") or "") != self.query:
            return True
        if (existing.get("tags") or "") != self.tags:
            return True
        if (existing.get("name") or None) != (self.name or None):
            return True
        if (existing.get("verb") or None) != (self.verb or None):
            return True
        if (existing.get("scope") or None) != (self.scope or None):
            return True
        if (existing.get("parent_dims") or None) != (self.parent_dims or None):
            return True
        if not _json_equal(existing.get("plan_json"), self.plan_json):
            return True
        return not _json_equal(
            existing.get("default_bindings"), self.default_bindings
        )


def desired_row_for_template(template: dict[str, Any]) -> DesiredRow:
    """Build the library row *template* describes.

    Binding declarations are folded into ``plan_json`` here (nexus-80tk):
    the YAML author declares ``required_bindings`` / ``optional_bindings``
    at the top level, and without this merge they never reach the DB —
    ``_validate_bindings`` then sees an empty list and lets unfilled
    ``$var`` placeholders leak into operator prompts.
    """
    dimensions = template["dimensions"]
    plan_json_payload: dict[str, Any] = dict(template["plan_json"])
    if template.get("required_bindings"):
        plan_json_payload["required_bindings"] = list(
            template["required_bindings"]
        )
    if template.get("optional_bindings"):
        plan_json_payload["optional_bindings"] = list(
            template["optional_bindings"]
        )
    return DesiredRow(
        query=template["description"],
        plan_json=json.dumps(plan_json_payload),
        tags=template.get("tags", "") or "",
        name=template.get("name"),
        verb=dimensions.get("verb"),
        scope=dimensions.get("scope"),
        default_bindings=(
            json.dumps(template["default_bindings"])
            if template.get("default_bindings") else None
        ),
        parent_dims=(
            canonical_dimensions_json(template["parent"])
            if template.get("parent") else None
        ),
    )


#: Tags that mark a row as user-grown rather than template-seeded. The
#: reconcile path never overwrites one. Grown plans are the only plans in
#: this library that currently match anything (~68% of hits), so a
#: reconcile that keyed on the wrong identity and clobbered them would
#: take the working path down with it.
_GROWN_TAGS: frozenset[str] = frozenset({"grown", "ad-hoc"})


def _protected_reason(row: dict[str, Any]) -> str | None:
    """Return why *row* must not be overwritten, or ``None`` if it may be."""
    tags = {t.strip() for t in (row.get("tags") or "").split(",") if t.strip()}
    hit = sorted(tags & _GROWN_TAGS)
    if hit:
        return (
            f"library row id={row.get('id')} is tagged {'/'.join(hit)} "
            "(user-grown); refusing to overwrite it from disk"
        )
    return None


def _json_equal(stored: str | None, desired: str | None) -> bool:
    """Compare two JSON payloads by VALUE, not by bytes.

    ``plan_json`` and ``default_bindings`` are ``jsonb`` columns, so PG
    hands back its own canonicalisation — reordered keys, normalised
    whitespace. A byte comparison against ``json.dumps`` output would
    report drift on every single row forever.
    """
    if stored is None or desired is None:
        return stored == desired
    try:
        return json.loads(stored) == json.loads(desired)
    except (TypeError, ValueError):
        return stored == desired


def load_seed_directory(
    directory: Path,
    *,
    library: HttpPlanLibrary,
    registered_dimensions: set[str] | None = None,
    outcome: str = "success",
    file_filter: Any = None,
    scope_override: str | None = None,
    reconcile: bool = False,
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

    *reconcile* extends that contract with an update leg (nexus-f1mbo):
    an existing row whose content has drifted from its template is
    rewritten and reported under ``updated``; an unchanged row is still
    skipped, so a reconcile run over an already-current library performs
    zero writes. A row tagged ``grown``/``ad-hoc`` is never rewritten —
    it lands in ``protected`` instead. Rows on disk that are absent from
    the library insert under both settings; rows in the library that are
    absent from disk are left untouched under both. See the module
    docstring for why deletion is not the orphan policy.
    """
    result = SeedLoadResult()
    if not directory.exists():
        # stdlib logging, not structlog: keyword fields would raise TypeError
        # here, turning "the seed dir is absent" into a crash on the one path
        # that is supposed to degrade quietly.
        _log.info("seed_directory_missing: path=%r", str(directory))
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
        except Exception as exc:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
            result.errors.append((source, f"{type(exc).__name__}: {exc}"))
            continue

        project = _default_project_for_scope(
            template["dimensions"].get("scope", "")
        )
        desired = desired_row_for_template(template)

        existing = library.get_plan_by_dimensions(
            project=project, dimensions=canonical,
        )
        if existing is not None:
            if not reconcile or not desired.differs_from(existing):
                result.skipped_existing.append(path.name)
                continue
            protected_reason = _protected_reason(existing)
            if protected_reason is not None:
                result.protected.append((path.name, protected_reason))
                continue
            # save_plan upserts on (project, QUERY), not on dimensions, so a
            # changed description would INSERT and collide with the
            # dimensional unique index. Drop the stale row first in exactly
            # that case; an unchanged description upserts in place.
            if (existing.get("query") or "") != desired.query:
                library.delete_plan(int(existing["id"]))

        library.save_plan(
            query=desired.query,
            plan_json=desired.plan_json,
            outcome=outcome,
            tags=desired.tags,
            project=project,
            name=desired.name,
            verb=desired.verb,
            scope=desired.scope,
            dimensions=canonical,
            default_bindings=desired.default_bindings,
            parent_dims=desired.parent_dims,
        )
        if existing is None:
            result.inserted.append(path.name)
        else:
            result.updated.append(path.name)

    return result
