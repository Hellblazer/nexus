# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Owner-management commands for the ``nx catalog`` group (nexus-kgyoz seam 3).

Carved verbatim out of ``commands.catalog`` (the ~6.9k-line god module).
``register`` attaches ``owners`` (list registered owners) to the shared
``catalog`` group so ``nx catalog owners`` resolves exactly as before.

``dedupe-owners`` was REMOVED in nexus-i711w Stage 2 sub-stage C-store. It was
deep maintenance: it mutated through the local rich Catalog's low-level event
log and ``_db`` transactions, which is not expressible as a service call, so it
had no service-mode implementation and already refused there. With the local
catalog deleted there is no path left to implement, and Hal ruled it
unsupported rather than reimplemented (2026-07-29). Its only helper,
``nexus.catalog.dedupe``, had no other consumer and died with it.

Shared helpers stay in ``commands.catalog``; ``owners_cmd`` reaches
``_get_catalog`` through the module object (not a bound import) so the existing
``patch("nexus.commands.catalog._get_catalog", …)`` test seam keeps working.

``--census`` (nexus-7kl32) is a read-only diagnostic arm: it classifies every
registered repo owner's on-disk root as ``healthy`` / ``path_vanished`` /
``path_exists_empty`` / ``unreadable``, surfacing the dead-owner debris
population (bench-index sandboxes, throwaway probe checkouts, stale
worktrees) that ``nx doctor``'s git-hooks check used to render as a
signal-free green (nexus-9t86i).

``--execute deactivate`` (nexus-cw262) is the mutation arm the census always
needed: the engine now carries a ``catalog_owners.deactivated_at`` soft-delete
column and a ``POST /v1/catalog/owners/deactivate`` route (Liquibase
catalog-022, ``CatalogRepository.deactivateOwner`` / ``CatalogHandler.
handleOwnerDeactivate``), so a dead owner can actually stop re-surfacing in
every future doctor/census run instead of just being diagnosed.

Double-gated exactly like ``nx catalog reconcile-stale``'s mutation arms
(``--dry-run/--no-dry-run`` default True + ``--confirm``; a forgotten flag
reports, it never mutates) — ``--execute deactivate`` additionally REQUIRES
``--census`` (there is no bare ``nx catalog owners --execute deactivate ...``:
the classification report is the eligibility evidence, not an optional
preamble). ``--execute reactivate`` instead requires ``--owner <prefix>`` — a
single targeted undo needs no census pass (see UNDO AFFORDANCE below).

ELIGIBILITY (critic design note, T2 21455, binding): only ``path_vanished``
rows are ever deactivate-eligible. ``path_exists_empty`` and ``unreadable``
are excluded from every population this arm ever acts on — an
emptied-but-present directory is not proof of a dead owner, and an unreadable
one is actively LESS evidence than "vanished" (a permission error says
nothing about whether the path still holds real content). Widening either
population needs its own design decision, not a flag on this one.

CORROBORATING SIGNAL (critic design note, T2 21455, binding): each
``path_vanished`` candidate is enriched with root path, its classification
bucket, and doc/chunk-count evidence pulled from ``cat.by_owner(tumbler)`` (an
existing reader surface — no new engine route needed) before it is offered as
a mutation target. A candidate with ANY live document attached is EXCLUDED
from the default population with a named reason rather than gated behind an
extra flag: a transiently-unmounted network path can plausibly still own
live, correctly-registered documents, and the failure mode of silently
deactivating an owner whose documents are still being served (making them
vanish from doctor/census while the documents themselves remain queryable
and now orphaned-looking) is worse than requiring Hal to investigate and
deactivate that one by hand. See ``_corroborate`` / ``_eligible_and_excluded``.

THE RESIDUAL (substantive-critic round 3, T2 21467, CRITICAL — half-closed
without this): the corroboration layer above defends only the has-live-docs
case. ``catalog_owners`` carries NO ``created_at``/``updated_at`` column at
all (verified ``catalog-001-baseline.xml``), so a genuinely healthy but
TRANSIENTLY-UNMOUNTED owner (a network volume, an external drive, a
container mount that is simply not attached right now) with ZERO currently-
registered documents (a freshly registered repo mid-first-index, or one
whose docs were legitimately cleared) is WIRE-INDISTINGUISHABLE from actual
debris: both read ``path_vanished`` + ``doc_count=0``. This residual cannot
be closed by more corroboration signal — there is no timestamp to age-gate
it against, and inventing one is new DDL out of this bead's scope. It is
bounded and made honest instead, three ways:

1. UNDO AFFORDANCE — ``--execute reactivate --owner <prefix>`` (this diff)
   clears ``deactivated_at`` for one named owner, alongside the pre-existing
   automatic reactivate-on-reregister (``upsertOwner``'s ``ownerUpdateSet``
   unconditionally clears the flag on any live write). A misfire is a
   one-command recovery, not silent data loss.
2. DRY-RUN HONESTY — every ``path_vanished``/0-doc candidate row states the
   residual explicitly (see ``_RESIDUAL_NOTE`` / ``_residual_note``), and the
   same language is in this docstring and ``docs/cli-reference.md``.
3. VISIBILITY — ``--include-deactivated`` (this diff) surfaces deactivated
   owners in both the census and the plain listing, so "silently deactivated
   forever" becomes "auditable state an operator can see and undo".

With all three, a misfire is bounded (one owner, one command to undo,
discoverable after the fact) rather than a silent permanent exclusion.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click
import structlog

from nexus.repos import owner_deactivate_capability

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader  # noqa: F401 — PEP 563 deferred annotation use

_log = structlog.get_logger(__name__)

#: Classification buckets a repo owner's root path can land in
#: (nexus-7kl32). Order matches the human-report presentation order.
_CENSUS_CLASSES = ("healthy", "path_vanished", "path_exists_empty", "unreadable")

#: Itemized-row cap per bucket in the human report (mirrors the
#: reconcile-stale precedent's ``_CAP_ACTION``/``_CAP_INFO`` convention).
_CENSUS_ROW_CAP = 20

#: nexus-cw262: the mutation arms this bead ships. ``reactivate`` added in
#: round 3 (T2 21467 Critical mitigation (a)) as the undo affordance.
_MUTATION_ACTIONS = ("deactivate", "reactivate")

#: The exact command this module tells an operator to run for the mutation
#: arm — printed verbatim in both the human report and the JSON payload so
#: Hal can copy-paste it without transcription risk (bead directive: "Print
#: the exact command in the dry-run output").
_EXECUTE_COMMAND = "nx catalog owners --census --execute deactivate --no-dry-run --confirm"

#: Template for the per-owner undo command (round 3 Critical mitigation
#: (a)/(b)) — ``.format(tumbler=...)`` fills in the target.
_REACTIVATE_COMMAND_TEMPLATE = (
    "nx catalog owners --execute reactivate --owner {tumbler} --no-dry-run --confirm"
)


def _classify_owner_root(repo_root: str) -> str:
    """Classify a registered owner's on-disk root path (nexus-7kl32).

    Four states — a check that cannot confidently establish "healthy" must
    never silently default to it (the same honesty principle nexus-9t86i
    forced onto doctor's git-hooks rendering):

    * ``path_vanished``     — the root does not exist at all. The dead-owner
      debris population: bench-index sandboxes, throwaway probe checkouts,
      stale worktrees. The ONLY bucket the mutation arm ever acts on.
    * ``path_exists_empty`` — the root is still there but has been emptied
      out (contents removed, directory left behind). NOT mutation-eligible:
      an empty directory is not proof the owner is dead (nexus-cw262).
    * ``unreadable``        — the root's existence or contents could not be
      confirmed (e.g. a permission error). Distinct from ``healthy`` on
      purpose: an unreadable directory is not evidence of health, and is
      even LESS evidence of death than ``path_exists_empty`` — never
      mutation-eligible.
    * ``healthy``           — the root exists and has content.
    """
    p = Path(repo_root)
    try:
        exists = p.exists()
    except OSError:
        return "unreadable"
    if not exists:
        return "path_vanished"
    if p.is_dir():
        try:
            if not any(p.iterdir()):
                return "path_exists_empty"
        except OSError:
            return "unreadable"
    return "healthy"


def _residual_note(tumbler: str) -> str:
    """The THE RESIDUAL disclosure (module docstring), rendered per-candidate
    row so it is impossible to miss in either the human report or JSON.
    Names the exact undo command for THIS owner."""
    return (
        "RESIDUAL: 0 live documents + vanished path cannot be distinguished "
        "from a transiently-unmounted volume (catalog_owners has no "
        "created_at/updated_at to age-gate this). If this was a misfire: "
        "re-index the repo (auto-reactivates, same tumbler prefix) or run "
        f"`{_REACTIVATE_COMMAND_TEMPLATE.format(tumbler=tumbler)}`."
    )


def _corroborate(cat: "CatalogReader", rows: list[dict]) -> list[dict]:
    """Attach doc/chunk-count evidence to each ``path_vanished`` candidate.

    nexus-cw262 critic design note (T2 21455): a candidate row must carry
    corroborating signal before the mutation arm ships, so a transiently-
    unmounted path is visually distinguishable from genuinely dead debris.
    Uses ``cat.by_owner(tumbler)`` — an existing reader surface, no new
    engine route needed. A read failure is recorded (not swallowed) as
    ``doc_count=None``; the caller treats that the same as "has live docs"
    (excluded, named reason) — absence of evidence is not evidence of
    absence, same discipline as ``reconcile-stale``'s unresolvable-provenance
    bucket.

    THE RESIDUAL (module docstring, round 3 critique): this defends only
    the has-live-docs case. ``doc_count == 0`` is NOT proof of debris —
    see ``_residual_note``, threaded onto every such row by
    ``_eligible_and_excluded``.
    """
    enriched: list[dict] = []
    for row in rows:
        tumbler = row["tumbler"]
        try:
            docs = cat.by_owner(tumbler) if tumbler else []
        except Exception as exc:  # noqa: BLE001 — isolated per-owner: continue, report the failure on the row
            _log.warning("owners_census_corroboration_failed", tumbler=tumbler, error=str(exc))
            enriched.append({**row, "doc_count": None, "chunk_count": None, "evidence_error": str(exc)})
            continue
        doc_count = len(docs)
        chunk_count = sum(getattr(d, "chunk_count", 0) or 0 for d in docs)
        enriched.append({**row, "doc_count": doc_count, "chunk_count": chunk_count})
    return enriched


def _eligible_and_excluded(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split corroborated ``path_vanished`` rows into (eligible, excluded).

    nexus-cw262 critic design note (T2 21455), binding: a candidate with >0
    live documents (or whose corroborating read itself failed) is EXCLUDED
    from the default population with a named ``exclusion_reason`` — never
    gated behind an extra override flag. See the module docstring's
    CORROBORATING SIGNAL section for the full rationale.

    Every eligible row also carries ``residual_note`` (round 3 critique,
    mitigation (b)) — the THE RESIDUAL disclosure is not optional reading,
    it travels WITH the row in both human and JSON output.
    """
    eligible: list[dict] = []
    excluded: list[dict] = []
    for row in rows:
        doc_count = row.get("doc_count")
        if doc_count is None:
            excluded.append({**row, "exclusion_reason": "corroboration_read_failed"})
        elif doc_count > 0:
            excluded.append({
                **row,
                "exclusion_reason": f"{doc_count} live document(s) still attached "
                                     "(chunk_count total: "
                                     f"{row.get('chunk_count')})",
            })
        else:
            eligible.append({**row, "residual_note": _residual_note(str(row.get("tumbler") or ""))})
    return eligible, excluded


def _run_census(
    cat: "CatalogReader",
    *,
    as_json: bool,
    action: str | None,
    dry_run: bool,
    confirm: bool,
    include_deactivated: bool,
) -> None:
    """Read-only owner-root census (nexus-7kl32 arm a) plus the optional
    ``--execute deactivate``/``reactivate`` mutation arms (nexus-cw262 arm
    b). Never constructs a catalog writer unless ``action`` is given AND
    both ``--no-dry-run`` and ``--confirm`` are set — report-first, same
    discipline as ``reconcile-stale``'s default mode.

    ONE fetch, ``include_deactivated=True`` always (round 3 critique): the
    census needs the full set regardless of the CLI flag, both to compute
    the engine-capability signal (:func:`nexus.repos.owner_deactivate_
    capability` — a row from a pre-cw262 engine carries no ``deactivated_at``
    key at all, present-but-null on a capable one) and, when
    ``--include-deactivated`` is passed, to actually show the deactivated
    rows (round 3 mitigation (c), VISIBILITY). The classification buckets
    themselves are still built from ACTIVE owners only — a deactivated
    owner has already been handled and must not be re-offered as a fresh
    census candidate.
    """
    all_owners = cat.list_owners_by_type("repo", include_deactivated=True)
    capability = owner_deactivate_capability(all_owners)
    owners = [o for o in all_owners if not o.get("deactivated_at")]
    deactivated_owners = [o for o in all_owners if o.get("deactivated_at")]

    buckets: dict[str, list[dict]] = {c: [] for c in _CENSUS_CLASSES}
    no_root: list[dict] = []
    for o in owners:
        root = o.get("repo_root") or ""
        row = {
            "tumbler": o.get("tumbler_prefix"),
            "name": o.get("name"),
            "repo_root": root,
        }
        if not root:
            # Mirrors reconcile-stale's ``no_repo_root`` disposition: absence
            # of a root is not evidence the owner is dead, just that this
            # census cannot say anything about it either way.
            no_root.append(row)
            continue
        buckets[_classify_owner_root(root)].append(row)

    dead_count = len(buckets["path_vanished"]) + len(buckets["path_exists_empty"])

    # nexus-cw262: corroborate + split ONLY the path_vanished bucket — the
    # sole mutation-eligible classification (module docstring ELIGIBILITY).
    corroborated = _corroborate(cat, buckets["path_vanished"])
    eligible, excluded = _eligible_and_excluded(corroborated)

    if as_json:
        payload = {
            "total_repo_owners": len(owners),
            "no_repo_root": no_root,
            **buckets,
            "dead_owner_count": dead_count,
            # nexus-cw262 round-3 critique (T2 21467 Significant-2):
            # capability-honest, never a hardcoded "available" — see
            # nexus.repos.owner_deactivate_capability.
            "mutation_status": capability,
            "mutation_eligible": eligible,
            "mutation_excluded": excluded,
            "execute_command": _EXECUTE_COMMAND,
            "reactivate_command_template": _REACTIVATE_COMMAND_TEMPLATE,
        }
        if include_deactivated:
            payload["deactivated_owners"] = deactivated_owners
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(
            f"Owner-root census: {len(owners)} repo owner(s) examined "
            f"({len(no_root)} with no repo_root, skipped)."
        )
        for cls in _CENSUS_CLASSES:
            click.echo(f"  {cls:<18} {len(buckets[cls])}")

        for cls in ("path_vanished", "path_exists_empty", "unreadable"):
            rows = buckets[cls]
            if not rows:
                continue
            click.echo(f"\n{cls} ({len(rows)}):")
            for row in rows[:_CENSUS_ROW_CAP]:
                click.echo(f"    {row['tumbler'] or '':<10} {row['repo_root']}")
            if len(rows) > _CENSUS_ROW_CAP:
                click.echo(f"    ... and {len(rows) - _CENSUS_ROW_CAP} more")

        click.echo(
            f"\n{dead_count} dead owner(s) (path_vanished + path_exists_empty) "
            "are GC candidates."
        )
        _echo_mutation_report(eligible, excluded, capability)

        if include_deactivated:
            click.echo(f"\nDeactivated owners ({len(deactivated_owners)}):")
            for o in deactivated_owners[:_CENSUS_ROW_CAP]:
                click.echo(
                    f"    {o.get('tumbler_prefix', ''):<10} "
                    f"{o.get('repo_root', '')}  "
                    f"deactivated_at={o.get('deactivated_at')}  "
                    f"undo: {_REACTIVATE_COMMAND_TEMPLATE.format(tumbler=o.get('tumbler_prefix'))}"
                )
            if len(deactivated_owners) > _CENSUS_ROW_CAP:
                click.echo(f"    ... and {len(deactivated_owners) - _CENSUS_ROW_CAP} more")
        elif deactivated_owners:
            click.echo(
                f"\n{len(deactivated_owners)} owner(s) are currently deactivated "
                "(hidden from the buckets above). Pass --include-deactivated to see them."
            )

    if action is None:
        return

    will_act = _report_only_notice(dry_run, confirm)
    if action == "deactivate":
        _run_deactivate(cat, eligible, will_act=will_act, dry_run=dry_run)
    elif action == "reactivate":
        raise click.ClickException(
            "--execute reactivate does not use --census -- pass --owner "
            "<tumbler_prefix> instead (see the module help)."
        )


def _echo_mutation_report(eligible: list[dict], excluded: list[dict], capability: str) -> None:
    """Human-readable corroborated candidate report (nexus-cw262). Printed
    even when ``--execute`` is not given, so ``nx catalog owners --census``
    alone shows exactly what the mutation arm would target."""
    click.echo(
        f"\nDeactivate-eligible (path_vanished, 0 live documents): {len(eligible)}"
    )
    for row in eligible[:_CENSUS_ROW_CAP]:
        click.echo(
            f"    {row['tumbler'] or '':<10} {row['repo_root']}  "
            f"[path_vanished, doc_count={row['doc_count']}, chunk_count={row['chunk_count']}]"
        )
        click.echo(f"        {row['residual_note']}")
    if len(eligible) > _CENSUS_ROW_CAP:
        click.echo(f"    ... and {len(eligible) - _CENSUS_ROW_CAP} more")

    if excluded:
        click.echo(
            f"\nExcluded from deactivation despite path_vanished ({len(excluded)}):"
        )
        for row in excluded[:_CENSUS_ROW_CAP]:
            click.echo(
                f"    {row['tumbler'] or '':<10} {row['repo_root']}  "
                f"[{row['exclusion_reason']}]"
            )
        if len(excluded) > _CENSUS_ROW_CAP:
            click.echo(f"    ... and {len(excluded) - _CENSUS_ROW_CAP} more")

    # nexus-cw262 round-3 critique (T2 21467 Significant-2): the printed
    # command must not claim availability the connected engine cannot back.
    if capability == "available":
        click.echo(f"\nTo deactivate the {len(eligible)} eligible owner(s):")
        click.echo(f"  {_EXECUTE_COMMAND}")
    elif capability == "unavailable":
        click.echo(
            "\nThe --execute deactivate mutation arm requires an engine build "
            "carrying the nexus-cw262 owner-deactivate route (not yet deployed "
            "here) — re-run after the connected engine is upgraded. Command "
            f"once available: {_EXECUTE_COMMAND}"
        )
    else:  # "unknown" -- empty owners response, no signal either way
        click.echo(
            "\nWhether --execute deactivate is available on the connected "
            f"engine could not be confirmed this run. Command to try: {_EXECUTE_COMMAND}"
        )


def _report_only_notice(dry_run: bool, confirm: bool) -> bool:
    """Echo the report-only nudge when applicable. Returns whether to act.

    Mirrors ``reconcile_stale._report_only_notice`` exactly — same
    double-gate contract, same wording convention."""
    will_act = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually mutate owners."
        )
    return will_act


def _run_deactivate(cat: "CatalogReader", eligible: list[dict], *, will_act: bool, dry_run: bool) -> None:
    if not will_act:
        if dry_run:
            click.echo("\n(dry-run — no catalog writes performed.)")
        return

    if not eligible:
        click.echo("\nNo eligible owners to deactivate.")
        return

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    writer = _cat_cmd._get_catalog_writer()
    n = 0
    skipped: list[dict] = []
    failures: list[dict] = []
    try:
        for row in eligible:
            tumbler = row["tumbler"]
            root = row["repo_root"]

            # TOCTOU re-check (S1, round 3 critique T2 21467 -- the
            # reconcile_stale._assert_empty_manifest precedent: "runtime
            # re-check... classification invariant re-check" immediately
            # before the tombstone/deactivate write, not just once at
            # classification time). The candidate list can be long-lived
            # (built once, up front) while THIS loop runs; a path that
            # remounted or a document that got registered in the interim
            # must not be silently deactivated on stale evidence.
            current_bucket = _classify_owner_root(root)
            if current_bucket != "path_vanished":
                skipped.append({
                    "tumbler": tumbler,
                    "reason": f"re-check: now classified {current_bucket}, not path_vanished",
                })
                continue
            try:
                docs_now = cat.by_owner(tumbler) if tumbler else []
            except Exception as exc:  # noqa: BLE001 — isolated per-owner: skip, report at end
                skipped.append({"tumbler": tumbler, "reason": f"re-check read failed: {exc}"})
                continue
            if len(docs_now) > 0:
                skipped.append({
                    "tumbler": tumbler,
                    "reason": f"re-check: now has {len(docs_now)} live document(s)",
                })
                continue

            try:
                deactivated = writer.deactivate_owner(tumbler)
                if deactivated:
                    n += 1
            except Exception as exc:  # noqa: BLE001 — isolated per-owner: continue, report at end
                failures.append({"tumbler": tumbler, "error": str(exc)})
                _log.warning("owners_deactivate_failed", tumbler=tumbler, error=str(exc))
    finally:
        writer.close()

    click.echo(f"\nDone: deactivated {n} owner(s).")
    if skipped:
        click.echo(
            f"\n{len(skipped)} skipped at the immediate-pre-write re-check "
            "(eligibility changed since classification):"
        )
        for s in skipped[:_CENSUS_ROW_CAP]:
            click.echo(f"    {s['tumbler']}: {s['reason']}")
    if failures:
        click.echo(f"\n{len(failures)} failure(s):")
        for f in failures[:_CENSUS_ROW_CAP]:
            click.echo(f"    {f['tumbler']}: {f['error']}")
        raise click.exceptions.Exit(1)


def _run_reactivate(cat: "CatalogReader", owner_prefix: str, *, will_act: bool, dry_run: bool) -> None:
    """The undo affordance (round 3 critique T2 21467 Critical mitigation
    (a) / Significant-5): reactivate ONE named owner by tumbler_prefix.
    No census/eligibility machinery — reactivation is inherently safe (it
    only ever clears a flag; ``upsertOwner``'s auto-reactivate-on-write is
    the SAME direction, so this can never resurrect something that
    shouldn't exist)."""
    matched: dict | None = None
    try:
        for o in cat.list_owners(include_deactivated=True):
            if str(o.get("tumbler_prefix")) == owner_prefix:
                matched = o
                break
    except Exception as exc:  # noqa: BLE001 — lookup is best-effort context, not a gate
        click.echo(f"\nCould not look up current state for {owner_prefix}: {exc}")

    if matched is None:
        click.echo(f"\nOwner {owner_prefix} not found in the catalog.")
    else:
        state = "deactivated" if matched.get("deactivated_at") else "already active"
        click.echo(
            f"\nOwner {owner_prefix} ({matched.get('name', '')}): currently {state}."
        )

    if not will_act:
        if dry_run:
            click.echo("\n(dry-run — no catalog writes performed.)")
        click.echo(
            f"\nTo reactivate: {_REACTIVATE_COMMAND_TEMPLATE.format(tumbler=owner_prefix)}"
        )
        return

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    writer = _cat_cmd._get_catalog_writer()
    try:
        reactivated = writer.reactivate_owner(owner_prefix)
    except Exception as exc:  # noqa: BLE001 — single-owner op: report, exit non-zero
        click.echo(f"\nFailed to reactivate {owner_prefix}: {exc}")
        writer.close()
        raise click.exceptions.Exit(1) from exc
    writer.close()

    if reactivated:
        click.echo(f"\nDone: reactivated {owner_prefix}.")
    else:
        click.echo(
            f"\nNo change: {owner_prefix} was already active (or does not exist)."
        )


@click.command("owners")
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--census", "do_census", is_flag=True,
    help=(
        "Classify repo-owner root paths as healthy / path_vanished / "
        "path_exists_empty / unreadable (nexus-7kl32), with corroborating "
        "doc/chunk-count evidence for the deactivate-eligible population."
    ),
)
@click.option(
    "--include-deactivated", is_flag=True, default=False,
    help=(
        "Also show owners that were deactivated (nexus-cw262): visibility "
        "affordance so a past --execute deactivate stays auditable rather "
        "than silently permanent. Works with both --census and the plain "
        "listing."
    ),
)
@click.option(
    "--execute", "action",
    type=click.Choice(_MUTATION_ACTIONS),
    default=None,
    help=(
        "Mutation arm (nexus-cw262). 'deactivate' requires --census (acts on "
        "path_vanished owners with 0 live documents). 'reactivate' requires "
        "--owner <tumbler_prefix> (the undo affordance for a deactivate "
        "misfire, or any manual reversal)."
    ),
)
@click.option(
    "--owner", "owner_prefix", default=None,
    help="Target tumbler_prefix for --execute reactivate.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run (with --confirm) to mutate.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Required alongside --no-dry-run and --execute to actually mutate owners.",
)
def owners_cmd(
    as_json: bool, do_census: bool, include_deactivated: bool,
    action: str | None, owner_prefix: str | None, dry_run: bool, confirm: bool,
) -> None:
    """List registered owners, or run a root-path census with --census.

    \\b
    Mutation arms (nexus-cw262):
      --execute deactivate --no-dry-run --confirm
          Requires --census. Soft-deletes path_vanished owners with 0 live
          documents. THE RESIDUAL (see module docstring): a doc_count==0
          owner cannot be told apart from a transiently-unmounted volume --
          catalog_owners carries no created_at/updated_at to age-gate this.
          Bounded by the undo affordance below, dry-run disclosure on every
          candidate row, and --include-deactivated visibility.
      --execute reactivate --owner <tumbler_prefix> --no-dry-run --confirm
          The undo affordance. Clears deactivated_at for ONE named owner --
          the misfire-recovery path alongside the automatic reactivate a
          live re-registration (e.g. `nx index repo` on a remounted path)
          already performs with no separate command.
    """
    if action == "deactivate" and not do_census:
        raise click.ClickException(
            "--execute deactivate requires --census: the classification "
            "report is the eligibility evidence for the mutation arm, not "
            "an optional preamble."
        )
    if action == "reactivate" and not owner_prefix:
        raise click.ClickException(
            "--execute reactivate requires --owner <tumbler_prefix>."
        )
    if as_json and action is not None:
        raise click.ClickException(
            "--json cannot be combined with --execute: the mutation arms "
            "print plain-text action reports that would corrupt the JSON "
            "stdout contract. Run --json alone for the census, then "
            "--execute separately."
        )

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()

    if action == "reactivate":
        will_act = _report_only_notice(dry_run, confirm)
        _run_reactivate(cat, owner_prefix, will_act=will_act, dry_run=dry_run)
        return

    if do_census:
        _run_census(
            cat, as_json=as_json, action=action, dry_run=dry_run, confirm=confirm,
            include_deactivated=include_deactivated,
        )
        return

    owners = cat.list_owners(include_deactivated=include_deactivated)
    if as_json:
        data = [
            {
                "tumbler": o.get("tumbler_prefix"),
                "name": o.get("name"),
                "type": o.get("owner_type"),
                "repo_hash": o.get("repo_hash"),
                "description": o.get("description"),
                "next_seq": o.get("next_seq"),
                **({"deactivated_at": o.get("deactivated_at")} if include_deactivated else {}),
            }
            for o in owners
        ]
        click.echo(json.dumps(data, indent=2))
    else:
        for o in owners:
            deactivated_note = (
                f"  [deactivated_at={o.get('deactivated_at')}]"
                if include_deactivated and o.get("deactivated_at") else ""
            )
            click.echo(
                f"{o.get('tumbler_prefix', ''):<8} "
                f"{o.get('owner_type', ''):<10} "
                f"{o.get('name', '')}{deactivated_note}"
            )


def register(group: click.Group) -> None:
    """Attach the owner-management commands to the shared ``catalog`` group."""
    group.add_command(owners_cmd)
