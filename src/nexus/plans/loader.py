# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Four-tier scoped plan loader — RDR-078 P6 (nexus-05i.10).

Walks the canonical plan tiers in order, validates each YAML template
via :func:`nexus.plans.schema.validate_plan_template`, and upserts
into a :class:`~nexus.db.t2.plan_library.PlanLibrary` with the tier's
scope stamped on the ``project`` column.

Tiers:
  1. ``<plugin_root>/plans/builtin/*.yml`` → ``scope:global`` (stored
     with ``project=""``).
  2. ``<repo>/docs/rdr/rdr-<slug>.md`` paired with
     ``<repo>/docs/rdr/rdr-<slug>/plans.yml`` → ``scope:rdr-<slug>``
     (loaded only when the RDR's YAML frontmatter declares
     ``status: accepted`` or ``status: closed``).
  3. ``<repo>/.nexus/plans/*.yml`` (excluding ``_repo.yml``) →
     ``scope:project``.
  4. ``<repo>/.nexus/plans/_repo.yml`` → ``scope:repo``.

Scope mismatch policy: the tier's (path's) scope wins. A YAML file in
``.nexus/plans/`` that declares ``scope:global`` in its ``dimensions``
is stored as ``scope:project`` and a structured warning
``plan_scope_path_mismatch`` is logged naming the declared vs stored
scope.

Idempotency: every tier uses :func:`nexus.plans.seed_loader.
load_seed_directory`, which itself uses the ``UNIQUE (project,
dimensions)`` partial index as the dedup guard.

SC-14, SC-15.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nexus.db.t2.plan_library import PlanLibrary
from nexus.plans.schema import (
    PlanTemplateSchemaError,
    canonical_dimensions_json,
    validate_plan_template,
)
from nexus.plans.seed_loader import SeedLoadResult, load_seed_directory

__all__ = [
    "load_all_tiers",
    "ci_validate_plan_tree",
]

_log = logging.getLogger(__name__)

_RDR_FRONTMATTER = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL,
)
_RDR_SLUG_RE = re.compile(r"^rdr-(.+)\.md$")


# ── Scope-path mismatch helper ─────────────────────────────────────────────


def _load_tier(
    *,
    directory: Path,
    scope: str,
    project_label: str,
    library: PlanLibrary,
    file_filter: Any = None,
) -> SeedLoadResult:
    """Load one scope tier, normalising mismatched scope declarations.

    When a template declares a scope different from the path-implied
    *scope*, we log ``plan_scope_path_mismatch`` and pass the path
    scope as an in-memory override to :func:`load_seed_directory`.
    The user's YAML file is never mutated — path-wins is a load-time
    policy, not a disk rewrite. A subsequent ``nx plan lint`` (RDR-079
    scope) can offer to fix the declared scope interactively.
    """
    if not directory.exists():
        return SeedLoadResult()

    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix not in (".yml", ".yaml"):
            continue
        if file_filter is not None and not file_filter(path):
            continue

        try:
            template = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        if not isinstance(template, dict):
            continue
        dims = template.get("dimensions") or {}
        declared = dims.get("scope")
        if declared and declared != scope:
            _log.warning(
                "plan_scope_path_mismatch: file=%r declared=%r "
                "stored=%r (path wins per RDR-078 P6; file not mutated)",
                str(path), declared, scope,
            )

    return load_seed_directory(
        directory, library=library, outcome="success",
        file_filter=file_filter, scope_override=scope,
    )


def _filter_project_excluding_repo(path: Path) -> bool:
    return path.name != "_repo.yml"


def _filter_repo_only(path: Path) -> bool:
    return path.name == "_repo.yml"


# ── RDR frontmatter status ─────────────────────────────────────────────────


def _rdr_status(rdr_md: Path) -> str | None:
    """Return the ``status:`` value from the RDR's YAML frontmatter.

    Returns ``None`` when the frontmatter is missing, unparseable,
    or doesn't declare ``status``. Malformed frontmatter never raises
    — it's the loader's job to skip quietly.
    """
    try:
        text = rdr_md.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _RDR_FRONTMATTER.match(text)
    if match is None:
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except Exception:
        return None
    if isinstance(meta, dict):
        return str(meta.get("status") or "").strip() or None
    return None


# ── Public API ─────────────────────────────────────────────────────────────


def load_all_tiers(
    *,
    plugin_root: Path,
    repo_root: Path,
    library: PlanLibrary,
) -> dict[str, SeedLoadResult]:
    """Walk the four tiers in order and return ``{scope: result}``.

    Tiers that have no files produce no entry in the returned dict.
    """
    results: dict[str, SeedLoadResult] = {}

    # Tier 1 — global plugin seeds.
    global_dir = plugin_root / "plans" / "builtin"
    global_result = _load_tier(
        directory=global_dir, scope="global",
        project_label="", library=library,
    )
    if global_result.total_scanned or global_dir.exists():
        results["global"] = global_result

    # Tier 2 — per-RDR plans.
    rdr_dir = repo_root / "docs" / "rdr"
    if rdr_dir.exists():
        for rdr_md in sorted(rdr_dir.glob("rdr-*.md")):
            slug_match = _RDR_SLUG_RE.match(rdr_md.name)
            if not slug_match:
                continue
            slug = slug_match.group(1)
            status = _rdr_status(rdr_md)
            if status not in ("accepted", "closed"):
                _log.info(
                    "rdr_plans_skipped_draft: slug=%r status=%r",
                    slug, status,
                )
                continue
            rdr_plans_dir = rdr_dir / f"rdr-{slug}"
            if not rdr_plans_dir.is_dir():
                continue
            scope_name = f"rdr-{slug}"
            tier_result = _load_tier(
                directory=rdr_plans_dir, scope=scope_name,
                project_label=scope_name, library=library,
            )
            if tier_result.total_scanned or rdr_plans_dir.exists():
                results[scope_name] = tier_result

    # Tier 3 — project-scope.
    project_dir = repo_root / ".nexus" / "plans"
    project_result = _load_tier(
        directory=project_dir, scope="project",
        project_label="project", library=library,
        file_filter=_filter_project_excluding_repo,
    )
    if project_result.total_scanned or (
        project_dir.exists()
        and any(
            p.suffix in (".yml", ".yaml") and p.name != "_repo.yml"
            for p in project_dir.iterdir()
        )
    ):
        results["project"] = project_result

    # Tier 4 — repo umbrella.
    repo_result = _load_tier(
        directory=project_dir, scope="repo",
        project_label="repo", library=library,
        file_filter=_filter_repo_only,
    )
    if repo_result.total_scanned or (project_dir / "_repo.yml").exists():
        results["repo"] = repo_result

    return results


# ── CI schema check ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CIError:
    path: str
    message: str


def _load_registered_dimensions(plugin_root: Path) -> set[str] | None:
    """Read the canonical dimensions registry from ``nx/plans/dimensions.yml``.

    Returns the set of registered top-level dimension keys, or ``None`` if
    the file is missing/unreadable (in which case the caller should fall
    back to lenient validation).
    """
    path = plugin_root / "plans" / "dimensions.yml"
    if not path.exists():
        return None
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    return {k for k in doc.keys() if isinstance(k, str)}


def ci_validate_plan_tree(
    *,
    plugin_root: Path,
    repo_root: Path,
) -> int:
    """Validate every plan YAML under the four-tier tree.

    Returns 0 when every file validates; non-zero (and prints the
    offenders to stderr) when one or more files fail. Used by the
    GitHub Actions workflow ``plan-schema-check.yml`` (SC-15).

    Runs the validator in strict mode with the dimensions registry
    loaded from ``nx/plans/dimensions.yml`` so SC-19 (unknown dimension
    rejected at CI) is actually enforced.
    """
    import sys

    errors: list[_CIError] = []
    registered = _load_registered_dimensions(plugin_root)
    strict = registered is not None

    directories: list[Path] = []
    directories.append(plugin_root / "plans" / "builtin")
    rdr_dir = repo_root / "docs" / "rdr"
    if rdr_dir.exists():
        for rdr_md in sorted(rdr_dir.glob("rdr-*.md")):
            slug_match = _RDR_SLUG_RE.match(rdr_md.name)
            if not slug_match:
                continue
            status = _rdr_status(rdr_md)
            if status not in ("accepted", "closed"):
                continue
            peer_dir = rdr_dir / f"rdr-{slug_match.group(1)}"
            if peer_dir.is_dir():
                directories.append(peer_dir)
    directories.append(repo_root / ".nexus" / "plans")

    for d in directories:
        if not d.exists():
            continue
        for path in sorted(d.iterdir()):
            if not path.is_file() or path.suffix not in (".yml", ".yaml"):
                continue
            try:
                template = yaml.safe_load(path.read_text()) or {}
            except Exception as exc:
                errors.append(_CIError(str(path), f"YAML error: {exc}"))
                continue
            try:
                validate_plan_template(
                    template,
                    registered_dimensions=registered,
                    strict=strict,
                )
            except PlanTemplateSchemaError as exc:
                errors.append(_CIError(str(path), str(exc)))

    if errors:
        print("Plan schema validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  {err.path}: {err.message}", file=sys.stderr)
        return 1
    return 0
