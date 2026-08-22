# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx plan`` command group.

Day-2 operations against the plan library:

  - ``nx plan list``    Tabulate plans with origin / use_count / scope
  - ``nx plan show``    Full plan_json + dimensions + run history
  - ``nx plan delete``  Remove a plan row (with confirmation)
  - ``nx plan reseed``  Re-run the four-tier seed loader

The first four (nexus-la28) close the routine-ops gap that bit the
RDR-098 abstract-themes smoke run: an inline-planner-grown plan
shadowed a builtin during testing and the only remediation was raw
SQL. The ``disable`` subcommand from the bead defers to a follow-up
because it requires a ``disabled_at`` column migration; once that
lands, ``disable`` slots in next to ``delete``.
"""
from __future__ import annotations

import json as _json

import click


def _classify_origin(row: dict) -> str:
    """Heuristic origin label. nexus-7bwe tracks adding an explicit
    ``origin`` column to the plans T2 table so this stops inferring from
    tags + project; deferred until a cross-session origin-filter
    reliability complaint drives the change.

    - ``builtin``  — tags carry the ``builtin-template`` token (seeded
      from ``conexus/plans/builtin/*.yml`` by ``nx catalog setup``).
    - ``grown``    — ``project=='personal'`` with no recognisable user
      tag, the shape ``_nx_answer_plan_grow`` produces.
    - ``user``     — anything else (called via ``plan_save`` MCP tool
      or written by an ad-hoc skill).
    """
    tags = row.get("tags") or ""
    if "builtin-template" in tags:
        return "builtin"
    project = row.get("project") or ""
    if project == "personal" and not tags:
        return "grown"
    return "user"


@click.group()
def plan() -> None:
    """Plan library maintenance commands."""


# The `nx plan repair` group (6 subcommands) and its SQLite helper
# `_open_plans_db` were DELETED (nexus-i711w Stage 2 sub-stage A3): every
# subcommand body took a sqlite3.Connection into the deleted SQLite
# PlanLibrary's tables ([21098] verb fates: `nx plan repair` D). The live
# library is engine-served; content repairs are engine-side operations.


def _open_plan_library():
    """Open the plan library (HttpPlanLibrary — the only implementation).

    nexus-o02xe / RDR-179 Phase 1 routed this through the storage-backend
    facade so service-mode CLIs stopped reading the frozen pre-migration
    SQLite snapshot. The seam is now COLLAPSED (nexus-i711w Stage 2
    sub-stage A3): the SQLite PlanLibrary died with the store, so the
    engine-served library is the only arm left.
    """
    from nexus.db.t2.http_plan_library import HttpPlanLibrary  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db.t2.http_plan_library)

    try:
        return HttpPlanLibrary()
    except Exception as exc:  # noqa: BLE001 — endpoint-resolution failure surfaced as a clean CLI error, not a traceback
        raise click.ClickException(
            f"plans service unavailable: {exc}"
        ) from exc


def _hygiene_scan(library) -> list[dict]:
    """Scan the plan library for hygiene violations (nexus-vtp8h).

    Three classes, each with a named reason:
      * non-executable plan_json (unparseable, or fails the
        ``validate_plan_steps(require_steps=True)`` executable-DAG check —
        the bead-dump class the drift audit found 77/116 of);
      * null-verb rows (pre-fiovt legacy pollution — save-time refusal
        exists now, but migrated rows predate it);
      * always-failing rows (zero successes, >= 3 failures — the matcher
        skips them live; hygiene retires them durably).

    Read-only; returns ``[{id, query, reason}]``.
    """
    import json as _json  # noqa: PLC0415 — command-local import

    from nexus.plans.matcher import _is_always_failing  # noqa: PLC0415 — command-local import
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_steps  # noqa: PLC0415 — command-local import

    findings: list[dict] = []
    scan_limit = 10_000
    rows = library.list_plans(limit=scan_limit, include_disabled=False)
    if len(rows) >= scan_limit:
        # No silent caps (reviewer Low non-vacuity): a library at/over the
        # scan limit means this pass may be incomplete — say so.
        click.echo(
            f"warn: plan library returned {len(rows)} rows at the "
            f"{scan_limit}-row scan limit — hygiene scan may be incomplete"
        )
    for row in rows:
        pid = row.get("id")
        query = (row.get("query") or "")[:60]
        reason = None
        try:
            parsed = _json.loads(row.get("plan_json") or "")
            validate_plan_steps(parsed, require_steps=True)
        except (TypeError, ValueError) as exc:
            reason = f"not an executable DAG: {exc}"
        except PlanTemplateSchemaError as exc:
            reason = f"not an executable DAG: {exc}"
        if reason is None and not (row.get("verb") or "").strip():
            reason = "null-verb (pre-fiovt pollution; unmatchable by verb-filtered nx_answer)"
        if reason is None and _is_always_failing(row):
            reason = (
                f"always-failing ({row.get('failure_count')} failures, "
                "0 successes)"
            )
        if reason is not None:
            findings.append({"id": pid, "query": query, "reason": reason})
    return findings


def _hygiene_apply(library, findings: list[dict]) -> int:
    """Disable (never delete) each finding; returns the count disabled.

    Partial failure is surfaced per-plan (reviewer Medium): a finding whose
    disable returns False (e.g. deleted between scan and apply) is echoed,
    never silently folded into the aggregate count.
    """
    count = 0
    for f in findings:
        reason = f"hygiene: {f['reason']}"[:120]
        if library.set_plan_disabled(f["id"], reason=reason):
            count += 1
        else:
            click.echo(
                f"  warn: could not disable [{f['id']}] "
                "(deleted or already disabled between scan and apply?)"
            )
    return count


@plan.command("hygiene")
@click.option("--apply", "apply_", is_flag=True,
              help="Disable the flagged plans (default: report only).")
def hygiene_cmd(apply_: bool) -> None:
    """Scan the plan library for bead-dumps, null-verb rows, and
    always-failing plans; --apply DISABLES them (reversible via
    `nx plan enable` — never deletes).

    Routes through HttpPlanLibrary, so it cleans the live engine-served
    library (nexus-vtp8h). (Its one-time local-SQLite sibling, `nx plan
    repair`, died with the SQLite store — nexus-i711w sub-stage A3.)
    """
    library = _open_plan_library()
    if library is None:
        return
    findings = _hygiene_scan(library)
    if not findings:
        click.echo("Plan library clean: no bead-dumps, null-verb, or always-failing plans.")
        return
    click.echo(f"{len(findings)} plan(s) flagged:")
    for f in findings:
        click.echo(f"  [{f['id']}] {f['query']!r}: {f['reason']}")
    if not apply_:
        click.echo("\nReport only — re-run with --apply to disable them "
                   "(reversible via `nx plan enable <id>`).")
        return
    count = _hygiene_apply(library, findings)
    click.echo(f"Disabled {count} plan(s).")


@plan.command("list")
@click.option(
    "--scope",
    default="",
    help="Filter by scope (global / personal / rdr-<slug> / project).",
)
@click.option(
    "--origin",
    type=click.Choice(["builtin", "grown", "user"], case_sensitive=False),
    default=None,
    help="Filter by inferred origin (heuristic; nexus-7bwe tracks an explicit origin column).",
)
@click.option(
    "--name",
    "name_pat",
    default="",
    help="Substring match against the plan ``name`` column.",
)
@click.option(
    "--limit",
    "-n",
    default=50,
    type=int,
    help="Max rows (default 50).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit JSON instead of a table.",
)
@click.option(
    "--include-disabled",
    is_flag=True,
    default=False,
    help="Include soft-disabled rows (nexus-mrzp). Default: skip.",
)
def list_cmd(
    scope: str, origin: str, name_pat: str, limit: int, as_json: bool,
    include_disabled: bool,
) -> None:
    """Tabulate plans in the library.

    \b
    Origin is heuristic (nexus-7bwe tracks the explicit ``origin`` column):
      - ``builtin``  tags include ``builtin-template``
      - ``grown``    project=='personal' AND empty tags
      - ``user``     everything else

    Examples:
      nx plan list
      nx plan list --scope=global --origin=builtin
      nx plan list --name=hybrid
    """
    lib = _open_plan_library()
    if lib is None:
        return
    try:
        # list_plans already filters out TTL-expired rows.
        rows = lib.list_plans(
            limit=max(limit * 4, limit),
            project="",
            include_disabled=include_disabled,
        )
    finally:
        lib.close()

    # Apply post-filters in Python so the heuristic origin filter
    # doesn't leak into the storage layer (where it doesn't exist).
    filtered = []
    for r in rows:
        if scope and (r.get("scope") or "") != scope:
            continue
        if name_pat and name_pat.lower() not in (r.get("name") or "").lower():
            continue
        if origin and _classify_origin(r) != origin.lower():
            continue
        filtered.append(r)
        if len(filtered) >= limit:
            break

    if as_json:
        click.echo(_json.dumps(
            [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "verb": r.get("verb"),
                    "scope": r.get("scope"),
                    "origin": _classify_origin(r),
                    "use_count": r.get("use_count"),
                    "last_used": r.get("last_used"),
                    "match_count": r.get("match_count"),
                }
                for r in filtered
            ],
            indent=2,
        ))
        return

    if not filtered:
        click.echo("No plans match.")
        return

    click.echo(
        f"{'id':>5}  {'origin':<8}  {'verb':<14}  {'scope':<10}  "
        f"{'use':>4}  {'last_used':<20}  name"
    )
    click.echo("  " + "-" * 80)
    for r in filtered:
        last = (r.get("last_used") or "")[:19] or "-"
        # nexus-mrzp: visually mark soft-disabled rows when the
        # operator opts in via --include-disabled.
        disabled_marker = "[D]" if r.get("disabled_at") else ""
        name_field = r.get("name") or r.get("query") or ""
        if disabled_marker:
            name_field = f"{disabled_marker} {name_field}"
        click.echo(
            f"{r.get('id') or 0:>5}  "
            f"{_classify_origin(r):<8}  "
            f"{(r.get('verb') or '-')[:14]:<14}  "
            f"{(r.get('scope') or '-')[:10]:<10}  "
            f"{r.get('use_count') or 0:>4}  "
            f"{last:<20}  "
            f"{name_field}"
        )


@plan.command("show")
@click.argument("id_or_name")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the full row as JSON.",
)
def show_cmd(id_or_name: str, as_json: bool) -> None:
    """Print a plan's full record (json + dimensions + run metrics).

    \b
    Argument may be a numeric id or a name substring (first match wins).
    """
    lib = _open_plan_library()
    if lib is None:
        return
    try:
        row = None
        if id_or_name.isdigit():
            row = lib.get_plan(int(id_or_name))
        if row is None:
            # Fallback: name-substring lookup over the active plans.
            for cand in lib.list_plans(limit=200):
                if id_or_name.lower() in (cand.get("name") or "").lower():
                    row = cand
                    break
    finally:
        lib.close()

    if row is None:
        click.echo(f"No plan matches {id_or_name!r}.")
        raise click.exceptions.Exit(1)

    if as_json:
        click.echo(_json.dumps(row, indent=2, default=str))
        return

    click.echo(f"id          {row.get('id')}")
    click.echo(f"name        {row.get('name') or '-'}")
    click.echo(f"origin      {_classify_origin(row)}")
    click.echo(f"verb        {row.get('verb') or '-'}")
    click.echo(f"scope       {row.get('scope') or '-'}")
    click.echo(f"project     {row.get('project') or '-'}")
    click.echo(f"created_at  {row.get('created_at') or '-'}")
    click.echo(f"last_used   {row.get('last_used') or '-'}")
    click.echo(f"use_count   {row.get('use_count') or 0}")
    click.echo(f"match_count {row.get('match_count') or 0}")
    click.echo(f"success     {row.get('success_count') or 0}")
    click.echo(f"failure     {row.get('failure_count') or 0}")
    click.echo(f"tags        {row.get('tags') or ''}")
    dims = row.get("dimensions") or "-"
    click.echo(f"dimensions  {dims}")
    click.echo("\nplan_json:")
    raw = row.get("plan_json") or ""
    try:
        click.echo(_json.dumps(_json.loads(raw), indent=2))
    except (ValueError, TypeError):
        click.echo(raw)


@plan.command("delete")
@click.argument("plan_id", type=int)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt.",
)
def delete_cmd(plan_id: int, yes: bool) -> None:
    """Delete the plan row identified by *plan_id*.

    \b
    The numeric id is required (not a name) because deletion is
    destructive and a name-substring lookup is fuzzy. Use ``nx plan
    list`` or ``nx plan show <name>`` to find the id first.
    """
    lib = _open_plan_library()
    if lib is None:
        return
    try:
        row = lib.get_plan(plan_id)
        if row is None:
            click.echo(f"No plan with id {plan_id}.")
            raise click.exceptions.Exit(1)

        label = row.get("name") or row.get("query") or "(unnamed)"
        if not yes:
            click.confirm(
                f"Delete plan id={plan_id} name={label!r}?",
                abort=True,
            )

        removed = lib.delete_plan(plan_id)
    finally:
        lib.close()

    click.echo(f"Removed {removed} row(s).")


@plan.command("disable")
@click.argument("plan_id", type=int)
@click.option(
    "--reason",
    default="",
    help="Optional reason; appended as a 'disable-reason:<text>' tag "
    "so the operator can later see why the plan was retired.",
)
def disable_cmd(plan_id: int, reason: str) -> None:
    """Soft-disable the plan with *plan_id* (nexus-mrzp).

    \b
    Soft-disable takes a plan out of rotation without deleting the row,
    preserving run history and supporting A/B tests, regression triage,
    and rollback. Re-enable with ``nx plan enable <id>``.

    \b
    Both matcher lanes (T1 cosine via list_active_plans, T2 full-text via
    search_plans) skip rows with disabled_at set.
    """
    lib = _open_plan_library()
    if lib is None:
        raise click.exceptions.Exit(1)
    try:
        row = lib.get_plan(plan_id)
        if row is None:
            click.echo(f"No plan with id {plan_id}.")
            raise click.exceptions.Exit(1)
        ok = lib.set_plan_disabled(plan_id, reason=reason)
    finally:
        lib.close()

    if not ok:
        click.echo(f"Failed to disable plan {plan_id}.")
        raise click.exceptions.Exit(1)

    label = row.get("name") or row.get("query") or "(unnamed)"
    suffix = f" (reason: {reason})" if reason else ""
    click.echo(f"Disabled plan id={plan_id} name={label!r}{suffix}.")


@plan.command("enable")
@click.argument("plan_id", type=int)
def enable_cmd(plan_id: int) -> None:
    """Re-enable a previously soft-disabled plan (nexus-mrzp).

    Clears the ``disabled_at`` column. The ``disable-reason:`` tag, if
    present, is preserved as a historical record.
    """
    lib = _open_plan_library()
    if lib is None:
        raise click.exceptions.Exit(1)
    try:
        row = lib.get_plan(plan_id)
        if row is None:
            click.echo(f"No plan with id {plan_id}.")
            raise click.exceptions.Exit(1)
        ok = lib.set_plan_enabled(plan_id)
    finally:
        lib.close()

    if not ok:
        click.echo(f"Failed to enable plan {plan_id}.")
        raise click.exceptions.Exit(1)

    label = row.get("name") or row.get("query") or "(unnamed)"
    click.echo(f"Enabled plan id={plan_id} name={label!r}.")


@plan.command("set-scope")
@click.argument("plan_id", type=int)
@click.argument("tags", required=False, default="")
@click.option(
    "--from-project",
    is_flag=True,
    help="Stamp scope_tags from the plan's own ``project`` column.  "
    "Mutually exclusive with the TAGS positional argument.",
)
def set_scope_cmd(plan_id: int, tags: str, from_project: bool) -> None:
    """Set or override the scope_tags for *plan_id*.

    \b
    This is an explicit admin override: it can widen or narrow scope.
    Use deliberately — the matcher reads scope_tags fresh on every call,
    so changes take effect immediately without a cache rebuild.

    \b
    Applies the same normalization as ``save_plan``: each comma-separated
    entry is stripped of hash suffixes and glob tails, scope-agnostic
    sentinels (``all``) are dropped, and the result is stored sorted-unique.
    Idempotent.

    \b
    With --from-project, stamps scope_tags from the plan's own
    ``project`` column — the same recovery source as the automatic
    #1069 fallback in ``save_plan`` (and the retired ``nx plan repair scope-tags``).

    \b
    Examples::

      nx plan set-scope 22 canon-conductor-compose
      nx plan set-scope 21 --from-project
      nx plan set-scope 43 rdr__arcaneum,knowledge__delos
    """
    if from_project and tags:
        raise click.UsageError(
            "--from-project and explicit TAGS are mutually exclusive."
        )
    if not from_project and not tags:
        raise click.UsageError(
            "Provide either TAGS or --from-project."
        )

    lib = _open_plan_library()
    if lib is None:
        return
    try:
        row = lib.get_plan(plan_id)
        if row is None:
            click.echo(f"No plan with id {plan_id}.")
            raise click.exceptions.Exit(1)

        if from_project:
            project = row.get("project") or ""
            from nexus.plans.scope import (  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.plans.scope)
                _SCOPE_AGNOSTIC_SENTINELS,
                _normalize_scope_string,
            )
            candidate = _normalize_scope_string(project.strip())
            resolved_tags = (
                candidate
                if candidate and candidate not in _SCOPE_AGNOSTIC_SENTINELS
                else ""
            )
        else:
            resolved_tags = tags

        ok = lib.set_scope_tags(plan_id, resolved_tags)
    finally:
        lib.close()

    if not ok:
        click.echo(f"Failed to update plan {plan_id}.")
        raise click.exceptions.Exit(1)

    label = row.get("name") or row.get("query") or "(unnamed)"
    # Derive the echo value from resolved_tags using the same normalization
    # that set_scope_tags applied.  No second connection needed — the
    # normalization is deterministic so this matches what was stored.
    from nexus.plans.scope import _SCOPE_AGNOSTIC_SENTINELS as _SAS, _normalize_scope_string as _nss  # noqa: PLC0415, N812
    parts = [
        _nss(p.strip())
        for p in resolved_tags.split(",")
        if p.strip() and p.strip() not in _SAS
    ]
    stored = ",".join(sorted({p for p in parts if p}))
    click.echo(
        f"Plan id={plan_id} name={label!r} scope_tags set to {stored!r}."
    )


@plan.command("reseed")
@click.option(
    "--force",
    "reconcile",
    is_flag=True,
    help="Also rewrite rows whose stored content has drifted from the "
    "template on disk. Without it the loader only inserts missing rows, "
    "so an edited description or plan_json never reaches an existing "
    "library.",
)
def reseed_cmd(reconcile: bool) -> None:
    """Re-run the four-tier plan-library seed loader.

    \b
    By default this is insert-only: previously-missing templates land,
    everything else is skipped. That is idempotent but also inert — the
    deduper keys on canonical dimensions, so an edited description or a
    replaced plan_json on an existing dimension is invisible to it, and
    a library seeded once stays frozen at whatever it first received
    (nexus-f1mbo).

    \b
    ``--force`` adds the update leg: each template is compared against
    its stored row and rewritten when they differ. A rewritten row is
    re-created, so its match/use counters reset — correct, since those
    counters described the superseded text. Rows tagged grown/ad-hoc
    are never rewritten, and library rows with no template on disk are
    left alone; both cases are reported rather than acted on.
    """
    # seed_plan_templates writes through T2Database.plans, which is
    # facade-routed (HttpPlanLibrary in service mode) — the seed half of
    # this verb is backend-correct in both modes.
    from nexus.commands.catalog import seed_plan_templates  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.commands.catalog)
    summary = seed_plan_templates(reconcile=reconcile)
    click.echo(f"Seeded {summary.inserted} new builtin row(s).")
    if reconcile:
        click.echo(f"Reconciled {summary.updated} drifted row(s).")
    elif summary.inserted == 0:
        click.echo(
            "Nothing inserted. If you edited a template, rerun with "
            "--force — the insert-only pass cannot see content drift."
        )
