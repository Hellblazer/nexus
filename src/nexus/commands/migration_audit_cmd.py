# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx migration-audit`` — retroactive target-name collision audit (nexus-p9vqa).

A deliberately SEPARATE command from ``nx migrate-to-service`` /
``nx guided-upgrade``: this one is strictly read-only forensics (an audit
mode hidden behind a flag on a destructive command is a footgun). See
:mod:`nexus.migration.collision_audit` for the verdict semantics.
"""
from __future__ import annotations

import json as _json
import sys
from typing import Any

import click


# RDR-185 P4.1 (nexus-n7u38.28): DEMOTED to an internal primitive — hidden
# from the user-facing surface, still callable + tested for surgical/dev use.
# Its job is the upgrade ladder's now (a diagnostic; `nx doctor` is the user surface).
# NOT deleted: hiding keeps scripts/surgical use working, and RDR-155 P4b
# owns the migration module's actual deletion (standing blocker).
@click.command(name="migration-audit", hidden=True)
@click.option("--local-path", default=None, help="Override the local Chroma path.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the machine-readable report instead of the rendered summary.",
)
@click.option(
    "--legs",
    type=click.Choice(["both", "local", "cloud"]),
    default="both",
    show_default=True,
    help=(
        "Which retained Chroma source legs to audit. Narrow ONLY when a leg "
        "is permanently gone (e.g. a retired ChromaCloud account) — the "
        "report and exit stay loudly partial-scope (nexus-ovbmb)."
    ),
)
@click.option(
    "--assume-voyage-key/--assume-no-voyage-key",
    "assume_voyage_key",
    default=None,
    help=(
        "Audit a single known migration history (a Voyage key was / was not "
        "configured when the store migrated). Default: audit BOTH worlds and "
        "probe the union — classification is voyage-key-dependent, and "
        "assuming today's key state can miss a merge that happened under "
        "yesterday's (nexus-772h2)."
    ),
)
def migration_audit_cmd(
    local_path: str | None,
    as_json: bool,
    legs: str,
    assume_voyage_key: bool | None,
) -> None:
    """Audit already-migrated pgvector targets for pre-guard silent merges.

    Re-runs the migration's own classification against the retained Chroma
    source, rebuilds the historical target-name map (under BOTH possible
    voyage-key histories unless --assume-* narrows it), and probes every
    pgvector target that two or more source collections would have written
    under one name — the collision class the nexus-5b9v0 guard now blocks
    up front, which a run predating that guard could hit SILENTLY (the
    overlapping-chash variant merges two collections with no error at all).

    Read-only on both stores. Exit codes: 0 = no collision groups exist;
    1 = flagged targets (see verdicts); 2 = at least one target was
    indeterminate (probe anomaly — re-run before trusting any verdict).

    CAVEAT for automated callers: exit 0 does NOT distinguish full-scope
    clean from partial-scope clean (a leg narrowed via --legs, or naturally
    absent). Scripts must inspect the JSON report's "partial_scope" /
    "audited_legs" fields — never gate on the exit code or "clean" alone.
    """
    from nexus.db.http_vector_client import get_http_vector_client  # noqa: PLC0415 — deferred import; http_vector_client only needed at run time
    from nexus.migration.collision_audit import (  # noqa: PLC0415 — command-local import (nexus.migration.collision_audit)
        audit_target_collisions,
    )

    # Same fail-loud engine-floor accessor convention as migrate-to-service
    # (nexus-b6qlf Fix 1): never a bare HttpVectorClient() construction.
    try:
        vector_client = get_http_vector_client()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    def _progress(target: str) -> None:
        if not as_json:
            click.echo(f"probing target {target!r} ...")

    try:
        report = audit_target_collisions(
            vector_client=vector_client,
            local_path=local_path,
            voyage_key_present=assume_voyage_key,
            legs=legs,
            on_progress=_progress,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(_json.dumps(_report_payload(report), indent=2, sort_keys=True))
    else:
        _render(report)

    if report.clean:
        return
    sys.exit(2 if report.indeterminate_targets else 1)


def _report_payload(report: Any) -> dict[str, Any]:
    return {
        "clean": report.clean,
        "requested_legs": report.requested_legs,
        "audited_legs": list(report.audited_legs),
        "partial_scope": report.partial_scope,
        "findings": [
            {
                "target": f.target,
                "target_exists": f.target_exists,
                "target_count": f.target_count,
                "union_source_ids": f.union_source_ids,
                "verdict": f.verdict,
                "detail": f.detail,
                "worlds": list(f.worlds),
                "sources": [
                    {
                        "collection": p.classification.collection,
                        "leg": p.classification.leg,
                        "model": p.classification.model,
                        "measured_dim": p.classification.measured_dim,
                        "reason": p.classification.reason,
                        "probed_ids": p.probed_ids,
                        "present_in_target": p.present_in_target,
                        "missing_from_target": p.missing_from_target,
                    }
                    for p in f.sources
                ],
            }
            for f in report.findings
        ],
    }


def _render(report: Any) -> None:
    if report.partial_scope:
        click.echo(
            f"⚠ PARTIAL SCOPE: audited leg(s) {list(report.audited_legs)} "
            f"(requested: {report.requested_legs}) — every verdict below, "
            "including 'clean', speaks ONLY for the audited leg(s)."
        )
    if report.clean:
        click.echo(
            "clean: no two source collections resolve to the same pgvector "
            "target — no pre-guard merge was possible on this store"
            + (" (within the audited leg scope)." if report.partial_scope else ".")
        )
        return
    click.echo(
        f"{len(report.findings)} collision target(s) flagged "
        f"({len(report.merged_targets)} merged, "
        f"{len(report.indeterminate_targets)} indeterminate):"
    )
    for f in report.findings:
        worlds = f" worlds: {', '.join(f.worlds)}" if f.worlds else ""
        click.echo(f"\n- target {f.target!r}  [{f.verdict}]{worlds}")
        click.echo(
            f"    rows in target: {f.target_count}   "
            f"union of source ids: {f.union_source_ids}"
        )
        for p in f.sources:
            c = p.classification
            # model / measured_dim / reason are the fields that tell the
            # operator WHICH colliding source is the pre-RDR-109 stale
            # mislabel vs the honest sibling (the 5b9v0 Fix-3 lesson) —
            # the human render must not hide them behind --json.
            measured = (
                f", measured {c.measured_dim}-dim" if c.measured_dim else ""
            )
            click.echo(
                f"    * {c.collection!r} ({c.leg}, model {c.model or '?'}"
                f"{measured}): {p.present_in_target}/{p.probed_ids} present, "
                f"{p.missing_from_target} missing"
            )
            if c.reason:
                click.echo(f"        {c.reason}")
        click.echo(f"    {f.detail}")
