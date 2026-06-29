# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Owner-management commands for the ``nx catalog`` group (nexus-kgyoz seam 3).

Carved verbatim out of ``commands.catalog`` (the ~6.9k-line god module):
``owners`` (list registered owners) and ``dedupe-owners`` (consolidate orphan
owners). Behaviour-preserving — the command names, options, and output are
identical; only the definition site moved. ``register`` attaches both to the
shared ``catalog`` group so ``nx catalog owners`` / ``nx catalog dedupe-owners``
resolve exactly as before.

Shared helpers stay in ``commands.catalog``; ``owners_cmd`` reaches
``_get_catalog`` through the module object (not a bound import) so the existing
``patch("nexus.commands.catalog._get_catalog", …)`` test seam keeps working.
"""
from __future__ import annotations

import json

import click


@click.command("owners")
@click.option("--json", "as_json", is_flag=True)
def owners_cmd(as_json: bool) -> None:
    """List registered owners."""
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    owners = cat.list_owners()
    if as_json:
        data = [
            {
                "tumbler": o.get("tumbler_prefix"),
                "name": o.get("name"),
                "type": o.get("owner_type"),
                "repo_hash": o.get("repo_hash"),
                "description": o.get("description"),
            }
            for o in owners
        ]
        click.echo(json.dumps(data, indent=2))
    else:
        for o in owners:
            click.echo(
                f"{o.get('tumbler_prefix', ''):<8} "
                f"{o.get('owner_type', ''):<10} "
                f"{o.get('name', '')}"
            )


@click.command("dedupe-owners")
@click.option("--apply", is_flag=True, default=False,
              help="Commit the plan. Default is dry-run.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit the plan as JSON instead of a human summary.")
def dedupe_owners_cmd(apply: bool, as_json: bool) -> None:
    """Consolidate orphan owners (nexus-tmbh, part of nexus-b34f).

    Classifies each curator owner as:

    \b
      • alias   — synthetic ``<repo>-<hash>`` names map to a canonical
                  repo owner. Each doc is aliased via documents.alias_of
                  to its canonical equivalent (matched by file_path).
                  Rows stay so external references keep resolving.
      • remove  — ``int-cce-*`` / ``int-prov-*`` / ``pdf-e2e-*`` test
                  leakage predating RDR-060's autouse fixture. Documents,
                  links, and the owner row are all deleted with JSONL
                  tombstones.
      • skip    — everything else (papers, knowledge, standalone-docs …).

    Dry-run by default. Use ``--apply`` to commit, then ``nx catalog
    sync`` to push the audit trail.
    """
    # Deep-maintenance: _dedupe.apply_plan mutates through the catalog's
    # low-level event log / _db transactions, not the 22 daemon write ops
    # (RDR-146). Use the full admin Catalog for both the plan read and the
    # apply write.
    from nexus.catalog.factory import (  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
        CatalogAdminDaemonLiveError,
        make_catalog_admin,
    )
    try:
        cat = make_catalog_admin()
    except CatalogAdminDaemonLiveError as exc:
        raise click.ClickException(str(exc)) from exc
    if cat is None:
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' first."
        )
    from nexus.catalog import dedupe as _dedupe  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    plan = _dedupe.plan_dedupe(cat)
    summary = plan.summary()

    if as_json:
        payload = {
            "dry_run": not apply,
            "summary": summary,
            "alias": [op.to_dict() for op in plan.alias],
            "remove": [op.to_dict() for op in plan.remove],
            "skip": [op.to_dict() for op in plan.skip],
        }
        if apply:
            payload["applied"] = _dedupe.apply_plan(cat, plan)
        click.echo(json.dumps(payload, indent=2))
        return

    label = "Would apply" if not apply else "Applying"
    click.echo(f"{label} dedupe plan:")
    click.echo(f"  alias:  {summary['alias']} owners, {summary['alias_docs']} docs")
    click.echo(f"  remove: {summary['remove']} owners, {summary['remove_docs']} docs")
    click.echo(f"  skip:   {summary['skip']} owners, {summary['skip_docs']} docs")

    def _section(title: str, items: list, show_canonical: bool = False) -> None:
        if not items:
            return
        click.echo(f"\n{title}:")
        for op in items:
            if show_canonical:
                click.echo(
                    f"  {op.orphan_prefix:<8} {op.orphan_name} "
                    f"({op.doc_count} docs) → {op.canonical_prefix} {op.canonical_name}"
                )
            else:
                click.echo(
                    f"  {op.orphan_prefix:<8} {op.orphan_name} "
                    f"({op.doc_count} docs)  — {op.reason}"
                )

    _section("Alias consolidation", plan.alias, show_canonical=True)
    _section("Unconditional removal", plan.remove)
    _section("Skipped (manual review)", plan.skip)

    if not apply:
        click.echo("\nDry-run only. Re-run with --apply to commit, "
                   "then `nx catalog sync` to push the audit trail.")
        return

    totals = _dedupe.apply_plan(cat, plan)
    click.echo("\nApplied:")
    click.echo(f"  orphans aliased:  {totals['orphans_aliased']} "
               f"({totals['aliased_docs']} docs, {totals['unmatched_docs']} unmatched)")
    click.echo(f"  orphans removed:  {totals['orphans_removed']} "
               f"({totals['removed_docs']} docs, {totals['removed_links']} links)")
    click.echo("\nRun `nx catalog sync` to commit and push the audit trail.")


def register(group: click.Group) -> None:
    """Attach the owner-management commands to the shared ``catalog`` group."""
    group.add_command(owners_cmd)
    group.add_command(dedupe_owners_cmd)
