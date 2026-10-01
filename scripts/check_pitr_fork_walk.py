#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Liquibase walk assertions for the conexus PITR-fork rehearsal (nexus-k9fs1).

The final engine cut carries the nexus-q81g7 schema pin: Liquibase's history
tables are pinned to ``public`` and the migration session's ``search_path`` is
pinned too. T2 ``nexus/review-q81g7-critique`` named what a rehearsal on a fork
of production must show, because no bare-box gate can reach it: the failing
property (a second walk that replans the world) only appears on a database that
has already been walked once, and a role named for a schema only appears with
the real role.

Two subcommands. Both exit 0 on pass, 1 on a failed assertion, 2 when the
evidence cannot be read (never a pass: an empty log, a psql that does not run).

``schema``  -- run BEFORE the walk, AFTER walk 1 and AFTER walk 2 against the fork:

    * exactly one ``databasechangelog`` and exactly one ``databasechangeloglock``
      in the whole database, both in ``public`` (pg_class, not
      information_schema, so a table the role cannot read still counts);
    * the changelog lock is not left held;
    * ``--migration-role`` is REQUIRED and must be non-empty (an empty value is
      exit 2, never a pass: ``--migration-role "$NX_DB_ADMIN_USER"`` expands to
      "" when that variable is unset). Take it from the engine's env, never from
      records: ``NX_DB_ADMIN_USER``, else ``NX_DB_USER`` (the engine defaults
      the admin user to the service user, Main.java). It is not ``nexus``,
      ``t1`` or ``staging`` and does not equal any schema in the database,
      the collision that split the history in the first place;
    * it prints every ``pg_db_role_setting`` row for the role and the database,
      because a role-level ``search_path`` is invisible from the changelog;
      ``--save-settings FILE`` records them (the BEFORE-the-walk run) and
      ``--compare-settings FILE`` fails when they differ (the AFTER-walk runs),
      so a walk that changes a role or database setting is caught;
    * ``--expect-rows N`` pins ``public.databasechangelog``'s row count (pass
      the count after walk 1 when checking after walk 2: a no-op walk adds none).

``walk``    -- run on the engine log of ONE walk (the log passed holds that boot):

    * a ``schema_migration_complete`` line exists, and no
      ``schema_migration_count_anomaly`` or ``schema_migration_failed``;
    * ``new_changesets + reexecuted_changesets + mark_ran_changesets ==
      pending_at_start`` (the identity SchemaMigrator logs the anomaly for);
    * ``schema_migration_session`` is present (proof the q81g7 engine ran, so
      a pre-fix engine cannot pass vacuously) and its role equals
      ``--migration-role`` when given; when the option is omitted the role is
      taken from that line (and reported as taken from the log), and an explicit
      empty value is exit 2;
    * a ``counts_unavailable=true`` line (the engine logs -1 for the three
      counts it could not compute) is exit 2: the counts cannot be read;
    * the log passed must hold exactly ONE boot (one ``schema_migration_start``): a
      file holding two boots would check only the second and hide the first, so more
      than one is exit 2;
    * one of ``--expect-new N`` or ``--noop`` is REQUIRED (neither is exit 2): a walk
      checked against neither proves only that the log is self-consistent, and a
      no-op boot of the new image passes that. Walk 1 of a tag that carries a
      changeset takes ``--expect-new N`` with N > 0 (``--expect-new 0`` is exit 2:
      that is ``--noop``, the second-walk property). Size N from the cloud's live
      ``release_version``, since the walk is cumulative;
    * ``--expect-new N`` pins ``new_changesets``; ``--noop`` is the second-walk
      property: ``new_changesets == 0``, ``mark_ran_changesets == 0`` and
      ``reexecuted_changesets == pending_at_start``, which is
      ``--expect-reexecuted`` (default :data:`DEFAULT_REEXECUTED`, the
      ``runAlways`` changesets of this changelog; a test counts them by XML parse
      so the constant cannot drift silently).

The connection comes from libpq's own environment (``PGHOST``, ``PGPORT``,
``PGUSER``, ``PGDATABASE``, ``PGPASSWORD`` or a service file) so no password is
ever on an argv; ``--psql`` names the binary. Nothing here prints the
environment.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

#: Role names that equal a schema the changelog creates; the migrating role must
#: not be one of them (review-q81g7-critique S4).
FORBIDDEN_ROLES: frozenset[str] = frozenset({"nexus", "t1", "staging"})

#: ``runAlways`` changesets the engine's changelog carries: the number a no-op
#: walk re-executes. Defined HERE, once; ``tests/scripts/test_check_pitr_fork_walk.py``
#: counts ``<changeSet runAlways="true">`` across the master changelog's includes
#: by XML parse and fails when this drifts, so bump it with the changelog.
DEFAULT_REEXECUTED = 12

HISTORY_TABLES: tuple[str, ...] = ("databasechangelog", "databasechangeloglock")

_KV = re.compile(r"(\w+)=(-?\d+)")
_SESSION = re.compile(
    r"event=schema_migration_session\s+role=(?P<role>\S+)\s+current_schema=(?P<schema>\S+)\s+"
    r"search_path=(?P<path>.*?)\s+pg_db_role_setting=(?P<settings>\[.*\])"
)

# A callable that takes SQL and returns rows of text columns.
Runner = Callable[[str], list[list[str]]]


class Unverifiable(Exception):
    """Evidence could not be read: exit 2, never a pass."""


def psql_runner(psql: str) -> Runner:
    def run(sql: str) -> list[list[str]]:
        try:
            proc = subprocess.run(
                [psql, "--no-psqlrc", "-X", "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                capture_output=True, text=True, check=False,
            )
        except OSError as exc:
            raise Unverifiable(f"cannot run {psql!r}: {exc}") from exc
        if proc.returncode != 0:
            # stderr can echo the connection target; report only its first line.
            first = (proc.stderr.strip().splitlines() or ["(no stderr)"])[0]
            raise Unverifiable(f"psql exited {proc.returncode}: {first}")
        return [line.split("|") for line in proc.stdout.splitlines() if line != ""]

    return run


def _require_role(migration_role: str | None) -> str:
    if migration_role is None or not migration_role.strip():
        raise Unverifiable(
            "--migration-role is empty or missing: the role-name check cannot run. "
            "Pass the engine's NX_DB_ADMIN_USER, else NX_DB_USER "
            "(an unset variable expands to an empty string)"
        )
    return migration_role.strip()


def check_schema(
    run: Runner,
    migration_role: str | None,
    expect_rows: int | None,
    save_settings: Path | None = None,
    compare_settings: Path | None = None,
) -> tuple[int, list[str]]:
    migration_role = _require_role(migration_role)
    lines: list[str] = []
    fails: list[str] = []

    for table in HISTORY_TABLES:
        rows = run(
            "select n.nspname from pg_catalog.pg_class c "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            f"where c.relname = '{table}' and c.relkind in ('r','p') order by 1"
        )
        schemas = [r[0] for r in rows]
        if schemas == ["public"]:
            lines.append(f"ok       exactly one {table}, in public")
        else:
            fails.append(f"{table} lives in {schemas or 'no schema'}; want exactly ['public']")

    locked = run("select locked from public.databasechangeloglock order by id")
    if not locked:
        fails.append("public.databasechangeloglock has no row")
    elif any(r[0] != "f" for r in locked):
        fails.append("public.databasechangeloglock is HELD (locked = true); a stuck walker")
    else:
        lines.append("ok       the changelog lock is not held")

    count_rows = run("select count(*) from public.databasechangelog")
    count = int(count_rows[0][0]) if count_rows and count_rows[0] else -1
    lines.append(f"info     public.databasechangelog rows = {count}")
    if expect_rows is not None and count != expect_rows:
        fails.append(f"public.databasechangelog has {count} rows, expected {expect_rows} (a no-op walk adds none)")

    user_schemas = {
        r[0] for r in run(
            "select nspname from pg_catalog.pg_namespace "
            "where nspname not like 'pg\\_%' and nspname <> 'information_schema'"
        )
    }
    if migration_role in FORBIDDEN_ROLES:
        fails.append(f"migration role {migration_role!r} is one of {sorted(FORBIDDEN_ROLES)}")
    elif migration_role in user_schemas:
        fails.append(f"migration role {migration_role!r} equals an existing schema {sorted(user_schemas)}")
    else:
        lines.append(f"ok       migration role {migration_role!r} names no schema")

    settings = run(
        "select coalesce(r.rolname, '(all roles)'), coalesce(d.datname, '(all databases)'), "
        "array_to_string(s.setconfig, ',') "
        "from pg_catalog.pg_db_role_setting s "
        "left join pg_catalog.pg_roles r on r.oid = s.setrole "
        "left join pg_catalog.pg_database d on d.oid = s.setdatabase "
        "order by 1, 2"
    )
    if settings:
        for role, db, conf in settings:
            lines.append(f"info     pg_db_role_setting role={role} database={db} {conf}")
            if "search_path" in conf:
                lines.append("note     a role/database search_path is set; the migration session pins its own, runtime sessions do not")
    else:
        lines.append("info     no pg_db_role_setting rows")

    # Canonical, order-stable form for the before/after comparison.
    canonical = sorted(list(map(str, row)) for row in settings)
    if save_settings is not None:
        try:
            save_settings.write_text(json.dumps(canonical))
        except OSError as exc:
            raise Unverifiable(f"cannot write --save-settings {save_settings}: {exc}") from exc
        lines.append(f"info     pg_db_role_setting rows saved to {save_settings} ({len(canonical)} row(s))")
    if compare_settings is not None:
        try:
            before = json.loads(compare_settings.read_text())
        except (OSError, ValueError) as exc:
            raise Unverifiable(f"cannot read --compare-settings {compare_settings}: {exc}") from exc
        if before == canonical:
            lines.append(f"ok       pg_db_role_setting rows unchanged since {compare_settings}")
        else:
            fails.append(
                f"pg_db_role_setting changed since {compare_settings}: before={before} now={canonical}"
            )

    return _verdict(lines, fails)


def parse_walk(log_text: str) -> dict[str, object]:
    """Counts and flags from the LAST walk in ``log_text``, plus how many walks it holds."""
    complete: dict[str, int] | None = None
    session: re.Match[str] | None = None
    anomaly = failed = unavailable = False
    starts = 0
    for line in log_text.splitlines():
        if "event=schema_migration_start" in line:
            starts += 1
            complete, session, anomaly, failed, unavailable = None, None, False, False, False
        if "event=schema_migration_complete" in line:
            complete = {k: int(v) for k, v in _KV.findall(line)}
            unavailable = "counts_unavailable=true" in line
        if "event=schema_migration_count_anomaly" in line:
            anomaly = True
        if "event=schema_migration_failed" in line:
            failed = True
        m = _SESSION.search(line)
        if m:
            session = m
    return {
        "complete": complete, "session": session, "anomaly": anomaly, "failed": failed,
        "counts_unavailable": unavailable, "starts": starts,
    }


def check_walk(
    log_text: str,
    migration_role: str | None,
    expect_new: int | None,
    noop: bool,
    expect_reexecuted: int,
) -> tuple[int, list[str]]:
    if not log_text.strip():
        raise Unverifiable("the engine log is empty")
    if migration_role is not None:
        migration_role = migration_role.strip()
    parsed = parse_walk(log_text)
    starts = parsed["starts"]
    assert isinstance(starts, int)
    if starts > 1:
        raise Unverifiable(
            f"the engine log holds {starts} boots (event=schema_migration_start x {starts}): "
            "pass ONE boot's log per file, or the check reads only the last boot and hides the others"
        )
    complete = parsed["complete"]
    if complete is None:
        raise Unverifiable("no event=schema_migration_complete line: the walk did not finish or the log is not the engine's")
    lines: list[str] = []
    fails: list[str] = []

    new = complete.get("new_changesets")
    rex = complete.get("reexecuted_changesets")
    mark = complete.get("mark_ran_changesets")
    pending = complete.get("pending_at_start")
    if None in (new, rex, mark, pending):
        raise Unverifiable(f"schema_migration_complete lacks a count field: {complete}")
    assert new is not None and rex is not None and mark is not None and pending is not None
    if parsed["counts_unavailable"] or min(new, rex, mark) < 0:
        raise Unverifiable(
            "the engine logged counts_unavailable (new/reexecuted/mark_ran = "
            f"{new}/{rex}/{mark}, the -1 sentinel): the walk's counts cannot be read, "
            "so the identity cannot be checked"
        )
    if expect_new is None and not noop:
        raise Unverifiable(
            "neither --expect-new N nor --noop was given: a walk checked against neither would pass a "
            "no-op boot of the new image. Walk 1 of a tag that carries a changeset takes --expect-new N "
            "(N > 0, sized from the cloud's live release_version); walk 2 takes --noop"
        )
    if expect_new is not None and expect_new <= 0 and not noop:
        raise Unverifiable(
            f"--expect-new {expect_new} asserts a walk that applies nothing, which is --noop; walk 1 of a "
            "schema-carrying tag must expect N > 0"
        )
    lines.append(f"info     walk: new={new} reexecuted={rex} mark_ran={mark} pending_at_start={pending}")

    if new + rex + mark == pending:
        lines.append("ok       new + reexecuted + mark_ran == pending_at_start")
    else:
        fails.append(f"new + reexecuted + mark_ran = {new + rex + mark} != pending_at_start {pending}")
    if parsed["anomaly"]:
        fails.append("the log carries event=schema_migration_count_anomaly")
    else:
        lines.append("ok       no schema_migration_count_anomaly event")
    if parsed["failed"]:
        fails.append("the log carries event=schema_migration_failed")

    session = parsed["session"]
    if session is None:
        fails.append("no event=schema_migration_session line: the engine under test predates the q81g7 walk-start diagnostics, so this proves nothing")
    else:
        assert isinstance(session, re.Match)
        lines.append(
            f"info     session: role={session['role']} current_schema={session['schema']} "
            f"search_path={session['path']} pg_db_role_setting={session['settings']}"
        )
        if migration_role is None:
            lines.append(
                f"info     --migration-role omitted: the role {session['role']!r} is taken from the engine log, "
                "so only the forbidden-name check applies to it"
            )
        elif session["role"] != migration_role:
            fails.append(f"the engine's session role {session['role']!r} != --migration-role {migration_role!r}")
        if session["role"] in FORBIDDEN_ROLES:
            fails.append(f"the engine's session role {session['role']!r} is one of {sorted(FORBIDDEN_ROLES)}")

    if expect_new is not None and new != expect_new:
        fails.append(f"new_changesets = {new}, expected {expect_new}")
    if noop:
        before = len(fails)
        if new != 0:
            fails.append(f"a second walk must apply nothing: new_changesets = {new}")
        if mark != 0:
            fails.append(f"a second walk must mark nothing: mark_ran_changesets = {mark}")
        if rex != expect_reexecuted:
            fails.append(f"a second walk re-executes the runAlways set: reexecuted_changesets = {rex}, expected {expect_reexecuted}")
        if pending != rex:
            fails.append(f"a second walk plans only the runAlways set: pending_at_start = {pending} != reexecuted {rex}")
        if len(fails) == before:
            lines.append(f"ok       second walk is a no-op: new=0 reexecuted={rex}")
    return _verdict(lines, fails)


def _verdict(lines: list[str], fails: list[str]) -> tuple[int, list[str]]:
    for f in fails:
        lines.append(f"FAILED   {f}")
    if fails:
        lines.append(f"FAILED: {len(fails)} assertion(s)")
        return 1, lines
    lines.append("PASSED")
    return 0, lines


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("schema", help="history tables, lock, role name, pg_db_role_setting")
    s.add_argument("--psql", default="psql")
    s.add_argument("--migration-role", default=None)
    s.add_argument("--expect-rows", type=int, default=None)
    s.add_argument("--save-settings", type=Path, default=None,
                   help="write the pg_db_role_setting rows to FILE (run before the walk)")
    s.add_argument("--compare-settings", type=Path, default=None,
                   help="fail when the pg_db_role_setting rows differ from FILE (run after a walk)")
    w = sub.add_parser("walk", help="the counts and events of one walk, from the engine log")
    w.add_argument("--engine-log", required=True, help="a file holding that boot's log, or - for stdin")
    w.add_argument("--migration-role", default=None)
    w.add_argument("--expect-new", type=int, default=None)
    w.add_argument("--noop", action="store_true", help="second-walk property")
    w.add_argument("--expect-reexecuted", type=int, default=DEFAULT_REEXECUTED)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "schema":
            rc, lines = check_schema(
                runner or psql_runner(args.psql), args.migration_role, args.expect_rows,
                args.save_settings, args.compare_settings,
            )
        else:
            if args.migration_role is not None:
                args.migration_role = _require_role(args.migration_role)
            text = sys.stdin.read() if args.engine_log == "-" else Path(args.engine_log).read_text()
            rc, lines = check_walk(text, args.migration_role, args.expect_new, args.noop, args.expect_reexecuted)
    except (Unverifiable, OSError) as exc:
        print(f"UNVERIFIABLE: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
