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
    * ``--migration-role`` (read it from the engine's env, never from records;
      cloud and local: the value of ``NX_DB_ADMIN_USER``) is not ``nexus``,
      ``t1`` or ``staging`` and does not equal any schema in the database,
      the collision that split the history in the first place;
    * it prints every ``pg_db_role_setting`` row for the role and the database,
      because a role-level ``search_path`` is invisible from the changelog;
    * ``--expect-rows N`` pins ``public.databasechangelog``'s row count (pass
      the count after walk 1 when checking after walk 2: a no-op walk adds none).

``walk``    -- run on the engine log of ONE walk (the log passed holds that boot):

    * a ``schema_migration_complete`` line exists, and no
      ``schema_migration_count_anomaly`` or ``schema_migration_failed``;
    * ``new_changesets + reexecuted_changesets + mark_ran_changesets ==
      pending_at_start`` (the identity SchemaMigrator logs the anomaly for);
    * ``schema_migration_session`` is present (proof the q81g7 engine ran, so
      a pre-fix engine cannot pass vacuously) and its role equals
      ``--migration-role`` when given;
    * ``--expect-new N`` pins ``new_changesets``; ``--noop`` is the second-walk
      property: ``new_changesets == 0``, ``mark_ran_changesets == 0`` and
      ``reexecuted_changesets == pending_at_start``, which is
      ``--expect-reexecuted`` (default 12, the ``runAlways`` changesets of this
      changelog; pass the real count if the changelog has grown).

The connection comes from libpq's own environment (``PGHOST``, ``PGPORT``,
``PGUSER``, ``PGDATABASE``, ``PGPASSWORD`` or a service file) so no password is
ever on an argv; ``--psql`` names the binary. Nothing here prints the
environment.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

#: Role names that equal a schema the changelog creates; the migrating role must
#: not be one of them (review-q81g7-critique S4).
FORBIDDEN_ROLES: frozenset[str] = frozenset({"nexus", "t1", "staging"})

#: ``runAlways`` changesets in this changelog when the bead was written.
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


def check_schema(
    run: Runner,
    migration_role: str | None,
    expect_rows: int | None,
) -> tuple[int, list[str]]:
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

    if migration_role:
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
    else:
        lines.append("info     no --migration-role given; the role-name check was NOT run")

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

    return _verdict(lines, fails)


def parse_walk(log_text: str) -> dict[str, object]:
    """Counts and flags from the LAST walk in ``log_text``."""
    complete: dict[str, int] | None = None
    session: re.Match[str] | None = None
    anomaly = failed = False
    for line in log_text.splitlines():
        if "event=schema_migration_start" in line:
            complete, session, anomaly, failed = None, None, False, False
        if "event=schema_migration_complete" in line:
            complete = {k: int(v) for k, v in _KV.findall(line)}
        if "event=schema_migration_count_anomaly" in line:
            anomaly = True
        if "event=schema_migration_failed" in line:
            failed = True
        m = _SESSION.search(line)
        if m:
            session = m
    return {"complete": complete, "session": session, "anomaly": anomaly, "failed": failed}


def check_walk(
    log_text: str,
    migration_role: str | None,
    expect_new: int | None,
    noop: bool,
    expect_reexecuted: int,
) -> tuple[int, list[str]]:
    if not log_text.strip():
        raise Unverifiable("the engine log is empty")
    parsed = parse_walk(log_text)
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
        if migration_role and session["role"] != migration_role:
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
    w = sub.add_parser("walk", help="the counts and events of one walk, from the engine log")
    w.add_argument("--engine-log", required=True, help="a file holding that boot's log, or - for stdin")
    w.add_argument("--migration-role", default=None)
    w.add_argument("--expect-new", type=int, default=None)
    w.add_argument("--noop", action="store_true", help="second-walk property")
    w.add_argument("--expect-reexecuted", type=int, default=DEFAULT_REEXECUTED)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "schema":
            rc, lines = check_schema(runner or psql_runner(args.psql), args.migration_role, args.expect_rows)
        else:
            text = sys.stdin.read() if args.engine_log == "-" else Path(args.engine_log).read_text()
            rc, lines = check_walk(text, args.migration_role, args.expect_new, args.noop, args.expect_reexecuted)
    except (Unverifiable, OSError) as exc:
        print(f"UNVERIFIABLE: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
