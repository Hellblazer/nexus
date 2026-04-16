# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx plan`` CLI — RDR-079 P6 (nexus-rxk).

Currently ships one subcommand: ``promote``. Additional lifecycle
subcommands (``list``, ``lint``) can be added to this Click group as
RDR-079 follow-up beads land.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import click
import yaml

from nexus.commands._helpers import default_db_path
from nexus.db.t2.plan_library import PlanLibrary
from nexus.plans.promote import (
    DEFAULT_MIN_DESCRIPTION_CHARS,
    DEFAULT_MIN_SUCCESS_RATE,
    DEFAULT_MIN_USE_COUNT,
    GateVerdict,
    evaluate_gates,
)

__all__ = ["plan"]


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, fallback: str) -> str:
    """Coerce arbitrary text into a filesystem-safe filename stem."""
    slug = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return slug or fallback


def _plugin_plans_builtin_dir() -> Path:
    """Return the plugin's global ``plans/builtin/`` directory.

    The nexus plugin lives at ``<repo>/nx/plans/builtin``. When run
    outside the nexus repo (i.e., users installing via PyPI without
    the nx plugin on disk), ``--plugin-root`` overrides this default.
    """
    return Path(__file__).resolve().parents[3] / "nx" / "plans" / "builtin"


def _target_dir(target: str, *, repo_root: Path, plugin_root: Path) -> Path:
    """Resolve the filesystem directory for a promotion *target* tier."""
    if target == "project":
        return repo_root / ".nexus" / "plans"
    if target == "global":
        return plugin_root / "plans" / "builtin"
    raise click.BadParameter(
        f"unknown target {target!r}; expected 'project' or 'global'",
    )


def _plan_row_to_yaml_doc(plan: dict[str, Any], *, target: str) -> dict[str, Any]:
    """Reshape a plan row into the YAML template shape consumed by the loader.

    The loader (``nexus.plans.loader``) reads ``dimensions``, ``steps``,
    and friends off the top-level dict. Inline the step body from
    ``plan_json`` and attach the scoped dimensions derived from the
    promotion target so a subsequent seed pass routes the file to the
    right tier.
    """
    body = json.loads(plan.get("plan_json") or "{}")
    dims_raw = plan.get("dimensions")
    if isinstance(dims_raw, str) and dims_raw:
        try:
            dims = json.loads(dims_raw)
        except json.JSONDecodeError:
            dims = {}
    else:
        dims = {}
    if not isinstance(dims, dict):
        dims = {}
    dims["scope"] = target  # target tier wins (scope/path symmetry)

    doc: dict[str, Any] = {
        "name": plan.get("name") or f"plan-{plan['id']}",
        "description": plan.get("query") or "",
        "dimensions": dims,
    }
    if "steps" in body:
        doc["steps"] = body["steps"]
    if "required_bindings" in body:
        doc["required_bindings"] = body["required_bindings"]
    if "optional_bindings" in body:
        doc["optional_bindings"] = body["optional_bindings"]
    default_bindings = plan.get("default_bindings")
    if isinstance(default_bindings, str) and default_bindings:
        try:
            parsed = json.loads(default_bindings)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed:
            doc["default_bindings"] = parsed
    return doc


def _emit_verdict(plan_id: int, verdict: GateVerdict, *, target: str) -> None:
    """Pretty-print a gate verdict to stdout."""
    plan = verdict.plan or {}
    use_count = plan.get("use_count", 0)
    success = plan.get("success_count", 0)
    failure = plan.get("failure_count", 0)
    total = success + failure
    rate = (success / total) if total else None
    click.echo(f"Plan: {plan_id}")
    click.echo(f"  name        : {plan.get('name') or '(unnamed)'}")
    click.echo(f"  target tier : {target}")
    click.echo(f"  use_count   : {use_count} (need ≥ {DEFAULT_MIN_USE_COUNT})")
    rate_str = f"{rate:.2f}" if rate is not None else "n/a"
    click.echo(
        f"  success_rate: {rate_str} "
        f"(need ≥ {DEFAULT_MIN_SUCCESS_RATE:.2f})",
    )
    desc_len = len(str(plan.get("query") or "").strip())
    click.echo(
        f"  description : {desc_len} chars "
        f"(need ≥ {DEFAULT_MIN_DESCRIPTION_CHARS})",
    )
    if verdict.passed:
        click.echo("  verdict     : PASS")
    else:
        click.echo("  verdict     : FAIL")
        for reason in verdict.reasons:
            click.echo(f"    - {reason}")


@click.group()
def plan() -> None:
    """Plan-library lifecycle commands."""


@plan.command("promote")
@click.argument("plan_id", type=int)
@click.option(
    "--target",
    type=click.Choice(["project", "global"], case_sensitive=False),
    required=True,
    help="Target tier: project → .nexus/plans/, global → plugin builtins.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report gate verdict without writing any file (SC-9).",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override T2 plan-library path (defaults to the user's memory.db).",
)
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Repo root for 'project' tier writes (defaults to cwd).",
)
@click.option(
    "--plugin-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Plugin root for 'global' tier writes (defaults to the bundled nx/).",
)
def promote(
    plan_id: int,
    target: str,
    dry_run: bool,
    db_path: Path | None,
    repo_root: Path | None,
    plugin_root: Path | None,
) -> None:
    """Promote a learned plan from the library up to ``--target`` tier.

    The gate must pass before any file is written. ``--dry-run`` reports
    the verdict without touching the filesystem (SC-9).
    """
    target = target.lower()
    library_path = db_path or default_db_path()
    library = PlanLibrary(library_path)
    try:
        verdict = evaluate_gates(library, plan_id)
        if dry_run:
            click.echo("DRY RUN — no files will be written.")
        _emit_verdict(plan_id, verdict, target=target)

        if not verdict.passed:
            sys.exit(1)

        if dry_run:
            click.echo("Gate passed. Re-run without --dry-run to promote.")
            return

        repo_root = repo_root or Path.cwd()
        plugin_root = plugin_root or _plugin_plans_builtin_dir().parents[1]
        repo_root = repo_root.resolve()
        plugin_root = plugin_root.resolve()

        # Guard against ``--repo-root ../../etc``-style path traversal.
        # We refuse to promote into a directory that isn't a git tree
        # (repo_root/.git present) unless it's the plugin_root itself
        # (global-tier writes).
        if target == "project" and not (repo_root / ".git").exists():
            raise click.ClickException(
                f"--repo-root {repo_root} is not a git working tree "
                f"(no .git directory). Refusing to promote into an "
                f"arbitrary path. Pass --repo-root pointing at the "
                f"repo whose .nexus/plans/ should receive the plan."
            )

        target_dir = _target_dir(
            target, repo_root=repo_root, plugin_root=plugin_root,
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        assert verdict.plan is not None  # passed ⇒ plan found
        doc = _plan_row_to_yaml_doc(verdict.plan, target=target)
        stem = _slugify(doc.get("name", ""), fallback=f"plan-{plan_id}")
        out_path = target_dir / f"{stem}.yml"
        # Refuse to overwrite an existing plan file — the slug is
        # deterministic and lossy, so "My Plan!" and "my plan" collide.
        # A silent overwrite here would drop a prior promotion.
        if out_path.exists():
            raise click.ClickException(
                f"{out_path} already exists. Rename the plan (name: "
                f"field in the row) or delete the existing file first. "
                f"Slug collisions silently overwriting prior promotions "
                f"is a data-loss footgun."
            )
        out_path.write_text(yaml.safe_dump(doc, sort_keys=False))
        click.echo(f"Promoted to {out_path}")
    finally:
        library.close()
