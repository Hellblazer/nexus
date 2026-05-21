# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx plan`` command group.

Day-2 operations against the plan library:

  - ``nx plan list``    Tabulate plans with origin / use_count / scope
  - ``nx plan show``    Full plan_json + dimensions + run history
  - ``nx plan delete``  Remove a plan row (with confirmation)
  - ``nx plan reseed``  Re-run the four-tier seed loader
  - ``nx plan repair``  Re-run the dimensional-identity backfill (RDR-092)

The first four (nexus-la28) close the routine-ops gap that bit the
RDR-098 abstract-themes smoke run: an inline-planner-grown plan
shadowed a builtin during testing and the only remediation was raw
SQL. The ``disable`` subcommand from the bead defers to a follow-up
because it requires a ``disabled_at`` column migration; once that
lands, ``disable`` slots in next to ``delete``.
"""
from __future__ import annotations

import json as _json
import sqlite3

import click


def _classify_origin(row: dict) -> str:
    """Heuristic origin label. nexus-7bwe tracks adding an explicit
    ``origin`` column to the plans T2 table so this stops inferring from
    tags + project; deferred until a cross-session origin-filter
    reliability complaint drives the change.

    - ``builtin``  — tags carry the ``builtin-template`` token (seeded
      from ``nx/plans/builtin/*.yml`` by ``nx catalog setup``).
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


@plan.group("repair")
def repair() -> None:
    """Consumer-side content-repair verbs for the plan library.

    Under RDR-120 §A8 the substrate's migration chain runs DDL only;
    legacy backfills that mutated row content moved out of
    ``apply_pending`` and into these explicit consumer-driven
    subcommands. Operators run them after `nx upgrade` (or whenever
    legacy rows surface that need repair). Each subcommand is
    idempotent.

    See RDR-120 §A8-exempt table for which substrate writes still
    stay in the migration chain.
    """


def _open_plans_db():
    """Open memory.db with WAL pragmas. Returns the connection, or
    None when the database does not exist."""
    from nexus.commands._helpers import default_db_path

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(f"T2 database not found at {db_path}; nothing to do.")
        return None
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@repair.command("scope-tags")
def repair_scope_tags_cmd() -> None:
    """Backfill empty ``scope_tags`` rows and rewash legacy ``'all'`` sentinels.

    Combines the substantive work of the pre-RDR-120 4.8.0 and 4.8.1
    migrations into one idempotent pass.
    """
    from nexus.plans.repair import repair_scope_tags

    conn = _open_plans_db()
    if conn is None:
        return
    try:
        result = repair_scope_tags(conn)
    finally:
        conn.close()
    _emit(result)


@repair.command("dimensions")
def repair_dimensions_cmd() -> None:
    """Backfill verb / name / dimensions on NULL-dimension plan rows.

    Heuristic verb inference; rows that reached the wh-fallback are
    tagged ``backfill-low-conf`` for operator review. Re-runs report
    "0 backfilled" once every row carries dimensions.
    """
    from nexus.plans.repair import repair_dimensions

    conn = _open_plans_db()
    if conn is None:
        return
    try:
        result = repair_dimensions(conn)
        # Surface low-confidence rows for operator review.
        low_conf_rows = conn.execute(
            "SELECT id, query, verb FROM plans "
            "WHERE tags LIKE '%backfill-low-conf%' "
            "ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    _emit(result)
    if low_conf_rows:
        click.echo(
            f"\n{len(low_conf_rows)} low-conf row(s) need review "
            "(tagged backfill-low-conf):"
        )
        for row_id, query, verb in low_conf_rows:
            click.echo(
                f"  id={row_id} verb={verb or '-'}  "
                f"query={(query or '').strip()!r}"
            )
    else:
        click.echo("\n0 rows need review.")


@repair.command("match-text")
def repair_match_text_cmd() -> None:
    """Populate ``plans.match_text`` (and refresh ``plans_fts``) for rows
    with empty ``match_text``. The schema (column + FTS5 table +
    triggers) was created by ``apply_pending``; this verb fills in
    the content the legacy migration used to populate.
    """
    from nexus.plans.repair import repair_match_text

    conn = _open_plans_db()
    if conn is None:
        return
    try:
        result = repair_match_text(conn)
    finally:
        conn.close()
    _emit(result)


@repair.command("retire-legacy")
def repair_retire_legacy_cmd() -> None:
    """Delete plan rows whose ``plan_json`` uses the pre-RDR-078
    ``operation`` shape. Such rows cannot be dispatched by ``plan_run``
    and only pollute plan-match results.
    """
    from nexus.plans.repair import repair_retire_legacy

    conn = _open_plans_db()
    if conn is None:
        return
    try:
        result = repair_retire_legacy(conn)
    finally:
        conn.close()
    _emit(result)


@repair.command("builtin-bindings")
def repair_builtin_bindings_cmd() -> None:
    """Patch ``required_bindings`` / ``optional_bindings`` into builtin
    plan rows whose stored ``plan_json`` predates the 4.10.1 seed
    loader fix.
    """
    from nexus.plans.repair import repair_builtin_bindings

    conn = _open_plans_db()
    if conn is None:
        return
    try:
        result = repair_builtin_bindings(conn)
    finally:
        conn.close()
    _emit(result)


@repair.command("all")
def repair_all_cmd() -> None:
    """Run every repair pass in dependency order."""
    from nexus.plans.repair import repair_all

    conn = _open_plans_db()
    if conn is None:
        return
    try:
        results = repair_all(conn)
    finally:
        conn.close()
    for name, result in results.items():
        click.echo(f"[{name}]")
        _emit(result, indent="  ")


def _emit(result: dict, *, indent: str = "") -> None:
    """Pretty-print a repair-verb result dict."""
    if not result:
        click.echo(f"{indent}(no-op)")
        return
    for key, value in result.items():
        click.echo(f"{indent}{key}: {value}")


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
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db.t2.plan_library import PlanLibrary  # noqa: PLC0415

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(f"T2 database not found at {db_path}.")
        return

    lib = PlanLibrary(path=db_path)
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
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db.t2.plan_library import PlanLibrary  # noqa: PLC0415

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(f"T2 database not found at {db_path}.")
        return

    lib = PlanLibrary(path=db_path)
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
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db.t2.plan_library import PlanLibrary  # noqa: PLC0415

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(f"T2 database not found at {db_path}.")
        return

    lib = PlanLibrary(path=db_path)
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
    Both matcher lanes (T1 cosine via list_active_plans, T2 FTS5 via
    search_plans) skip rows with disabled_at set.
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db.t2.plan_library import PlanLibrary  # noqa: PLC0415

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(f"T2 database not found at {db_path}.")
        raise click.exceptions.Exit(1)

    lib = PlanLibrary(path=db_path)
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
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db.t2.plan_library import PlanLibrary  # noqa: PLC0415

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(f"T2 database not found at {db_path}.")
        raise click.exceptions.Exit(1)

    lib = PlanLibrary(path=db_path)
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


@plan.command("reseed")
@click.option(
    "--force",
    is_flag=True,
    help="Delete every builtin row first so description / template "
    "changes pick up cleanly. Without --force the loader is idempotent "
    "and only inserts missing rows.",
)
def reseed_cmd(force: bool) -> None:
    """Re-run the four-tier plan-library seed loader.

    \b
    By default this is idempotent: only previously-missing builtins
    insert. Use ``--force`` when you've edited a builtin's description
    or replaced its plan_json — the deduper keys on canonical
    dimensions, so a description tweak on an existing dimension is
    invisible to the idempotent path.
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db.t2.plan_library import PlanLibrary  # noqa: PLC0415

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(f"T2 database not found at {db_path}.")
        return

    if force:
        lib = PlanLibrary(path=db_path)
        try:
            with lib._lock:
                cursor = lib.conn.execute(
                    "DELETE FROM plans "
                    "WHERE (',' || tags || ',') LIKE '%,builtin-template,%'"
                )
                lib.conn.commit()
                removed = cursor.rowcount
        finally:
            lib.close()
        click.echo(f"--force: removed {removed} builtin row(s).")

    from nexus.commands.catalog import _seed_plan_templates  # noqa: PLC0415
    seeded = _seed_plan_templates()
    click.echo(f"Seeded {seeded} new builtin row(s).")
