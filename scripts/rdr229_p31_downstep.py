#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17, residual R5 nexus-p2m1x.44): the operator down-step and its re-add, generated from the one list.

A pre-P3.1 engine cannot write a row the P3.1 CHECKs guard (a NOT VALID CHECK applies to every new row version): its chunk
INSERT, its supersede UPDATE and its carrier INSERTs fail with SQLSTATE 23514. The rollback after P3.1 is therefore this
down-step (DROP CONSTRAINT IF EXISTS on every listed constraint) first and the tag flip second, with the open stamp moves
paused before the down-step (the previous engine's move worker would otherwise run moves whose on_complete fails once the
CHECKs are gone). conexus runs it; the runbook is docs/runbooks/rdr-229-p31-rollback.md.

The down-step drops the CHECKs but DATABASECHANGELOG still records vectors-068-3 to -19 as run, so a later deploy of the
P3.1 engine does not add them again, and a stamp move's on_complete then fails on a constraint that does not exist (42704).
The re-add is the other half: the same list, the same expressions the changesets add them with, each NOT VALID (so it
scans no row), each guarded so it can run twice and skips a table or id column the install lacks. The one CHECK vectors-068-19
holds back while a tombstone with a blank model_version has no superseded_by_id (D2's repair move would meet 23514 on it) is
held back here on the same condition, with a NOTICE; the operator runs the re-add again once that move has completed. The
runbook runs it after the redeploy and before it releases the pause.

The constraint names come from service/src/main/resources/db/rdr229/p31-checks.tsv, the list the changesets, the
completion check and this script share, so a table added to it is added to both.

Usage: rdr229_p31_downstep.py [--root DIR] (--print | --print-readd | --write | --check)
  --print        the down-step SQL on stdout
  --print-readd  the re-add SQL on stdout
  --write        rewrite service/src/main/resources/db/rdr229/p31-downstep.sql, docs/runbooks/rdr-229-p31-readd.sql and
                 its classpath copy service/src/main/resources/db/rdr229/p31-readd.sql (the engine test runs the copy: a
                 changelog reference must stay under the hashed db/ tree)
  --check        exit 1 when any committed SQL file, or the down-step, the re-add, the verification query, the pause and
                 the unpause embedded in the runbook, differ from a fresh run (a drift check)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LIST_REL = "service/src/main/resources/db/rdr229/p31-checks.tsv"
SQL_REL = "service/src/main/resources/db/rdr229/p31-downstep.sql"
READD_REL = "docs/runbooks/rdr-229-p31-readd.sql"
READD_DB_REL = "service/src/main/resources/db/rdr229/p31-readd.sql"
RUNBOOK_REL = "docs/runbooks/rdr-229-p31-rollback.md"

DOWN_BEGIN = "<!-- p31-downstep:begin -->"
DOWN_END = "<!-- p31-downstep:end -->"
READD_BEGIN = "<!-- p31-readd:begin -->"
READD_END = "<!-- p31-readd:end -->"
VERIFY_BEGIN = "<!-- p31-verify:begin -->"
VERIFY_END = "<!-- p31-verify:end -->"
PAUSE_BEGIN = "<!-- p31-pause:begin -->"
PAUSE_END = "<!-- p31-pause:end -->"
UNPAUSE_BEGIN = "<!-- p31-unpause:begin -->"
UNPAUSE_END = "<!-- p31-unpause:end -->"

# What paused_by says, so the unpause releases only the rows this procedure paused and never an operator's own hold.
PAUSED_BY = "rdr229 down-step"

# vectors-068-19's precondition, word for word (tests/test_rdr229_p31_downstep.py compares it with the changeset text).
SUPERSEDED_HELD_BACK = "c.superseded_by <> '' AND c.superseded_by_id IS NULL AND c.model_version = ''"

_NAME = re.compile(r"^\w+$")


def parse_rows(text: str) -> list[tuple[str, str, str, str, str]]:
    """(table, name column, id column, constraint, kind) for every row that carries a CHECK, in file order. A malformed or
    empty list is refused."""
    rows: list[tuple[str, str, str, str, str]] = []
    seen_rows = 0
    for line in text.splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("table\t"):
            continue
        fields = line.split("\t")
        if len(fields) != 5:
            raise ValueError(f"p31-checks.tsv: a row has {len(fields)} columns, not five: {line!r}")
        seen_rows += 1
        table, name_column, id_column, constraint, kind = fields
        if constraint == "-":
            continue   # a history table: a bare nullable id, no CHECK, nothing to drop
        if not all(_NAME.match(f) for f in (table, id_column, constraint)) or not (name_column == "-" or _NAME.match(name_column)):
            raise ValueError(f"p31-checks.tsv: not a plain identifier: {line!r}")
        rows.append((table, name_column, id_column, constraint, kind))
    if seen_rows == 0:
        raise ValueError("p31-checks.tsv lists no row: refusing to generate an empty down-step")
    if not rows:
        raise ValueError("p31-checks.tsv lists no constraint: refusing to generate an empty down-step")
    if len({r[3] for r in rows}) != len(rows):
        raise ValueError("p31-checks.tsv names a constraint twice")
    return rows


def parse_list(text: str) -> list[tuple[str, str]]:
    """(table, constraint) for every row that carries a CHECK, in file order."""
    return [(table, constraint) for table, _, _, constraint, _ in parse_rows(text)]


def generate_down_step(list_text: str) -> str:
    rows = parse_list(list_text)
    lines = [
        "-- RDR-229 P3.1 operator down-step (residual R5, nexus-p2m1x.44). GENERATED by scripts/rdr229_p31_downstep.py from",
        "-- service/src/main/resources/db/rdr229/p31-checks.tsv: edit the list and run that script with --write, never this file.",
        "--",
        "-- Drops every CHECK RDR-229 P3.1 added, NOT VALID or validated, so a pre-P3.1 engine can write again. Run it as",
        "-- nexus_admin (the table owner), in one transaction, AFTER the open stamp moves are paused and BEFORE the previous",
        "-- engine's tag is flipped back in",
        "-- (docs/runbooks/rdr-229-p31-rollback.md):",
        "--   psql -X -v ON_ERROR_STOP=1 --single-transaction -f p31-downstep.sql",
        "-- It leaves the id columns, the registry's identity column and every row of the registry of moves alone, and it is",
        "-- safe to run twice. A table that is absent (the retired queue table on most installs) is skipped.",
        "SET LOCAL lock_timeout = '5s';",
    ]
    lines += [f"ALTER TABLE IF EXISTS nexus.{table} DROP CONSTRAINT IF EXISTS {constraint};" for table, constraint in rows]
    return "\n".join(lines) + "\n"


def check_expression(name_column: str, id_column: str, kind: str) -> str:
    """The expression vectors-068 adds the constraint with, by kind (tests/test_rdr229_p31_downstep.py compares it with the
    changeset text, so a change to either side is caught)."""
    if kind == "SUPERSEDED":
        return f"({name_column} = '') = ({id_column} IS NULL)"
    if kind == "NAME_ID":
        return f"(NULLIF({name_column}, '') IS NULL) = ({id_column} IS NULL)"
    if kind == "NOT_NULL":
        return f"{id_column} IS NOT NULL"
    raise ValueError(f"p31-checks.tsv: unknown kind {kind!r}")


def generate_readd(list_text: str) -> str:
    """Adds every listed CHECK back, NOT VALID, the way vectors-068 added it. One guarded block per constraint: a table or id
    column the install lacks is skipped, and a constraint already present is left alone, so it is safe to run twice."""
    lines = [
        "-- RDR-229 P3.1 operator re-add (fix round C, nexus-p2m1x.17). GENERATED by scripts/rdr229_p31_downstep.py from",
        "-- service/src/main/resources/db/rdr229/p31-checks.tsv: edit the list and run that script with --write, never this file.",
        "--",
        "-- Adds back every CHECK the down-step removed, each NOT VALID with the expression vectors-068 added it with. The",
        "-- changelog records those changesets as run, so a redeploy of the P3.1 engine does not add them again. Run it as",
        "-- nexus_admin (the table owner), in one transaction, AFTER the P3.1 engine is serving again and BEFORE the pause on",
        "-- the stamp moves is released (docs/runbooks/rdr-229-p31-rollback.md):",
        "--   psql -X -v ON_ERROR_STOP=1 --single-transaction -f rdr-229-p31-readd.sql",
        "-- A NOT VALID constraint scans no row, so rows the previous engine wrote meanwhile cannot fail it; the stamp moves",
        "-- fill their ids. It is safe to run twice. A table or id column that is absent (the retired queue) is skipped.",
        "-- catalog_collections_superseded_by_id_check is held back, with a NOTICE, while a tombstone with a blank model_version",
        "-- has no superseded_by_id (vectors-068-19's precondition): run this script again once D2's repair move has completed.",
        "SET LOCAL lock_timeout = '5s';",
    ]
    for table, name_column, id_column, constraint, kind in parse_rows(list_text):
        expression = check_expression(name_column, id_column, kind)
        add = f"ALTER TABLE nexus.{table} ADD CONSTRAINT {constraint} CHECK ({expression}) NOT VALID;"
        lines += [
            "DO $q$",
            "BEGIN",
            f"    IF to_regclass('nexus.{table}') IS NOT NULL",
            f"       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.{table}') AND attname = '{id_column}' AND NOT attisdropped)",
            f"       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.{table}') AND conname = '{constraint}') THEN",
        ]
        if kind == "SUPERSEDED":
            # vectors-068-19's data precondition: a NOT VALID CHECK refuses the UPDATE of an unstamped tombstone with 23514, and D2's
            # repair-catalog-collection-model-version move UPDATEs exactly the tombstones with a blank model_version. The changeset skips
            # the CHECK while one exists and retries at the next boot; this script skips it and says so, and the operator runs it again.
            lines += [
                f"        IF EXISTS (SELECT 1 FROM nexus.{table} c WHERE {SUPERSEDED_HELD_BACK}) THEN",
                f"            RAISE NOTICE '{constraint} skipped: a tombstone with a blank model_version has no {id_column}, and D2''s repair-catalog-collection-model-version move would meet 23514 on it; run this script again once that move has completed';",
                "        ELSE",
                f"            {add}",
                "        END IF;",
            ]
        else:
            lines += [f"        {add}"]
        lines += [
            "    END IF;",
            "END",
            "$q$;",
        ]
    return "\n".join(lines) + "\n"


def generate_verification(list_text: str) -> str:
    names = ", ".join(f"'{constraint}'" for _, constraint in parse_list(list_text))
    return (
        "SELECT conrelid::regclass AS relation, conname FROM pg_catalog.pg_constraint\n"
        f" WHERE conname IN ({names})\n"
        " ORDER BY conname, relation::text;\n"
    )


def generate_pause() -> str:
    """Holds every open stamp move, so the previous engine's move worker starts none of them (its on_complete would fail)."""
    return (
        f"UPDATE nexus.data_moves SET paused_at = now(), paused_by = '{PAUSED_BY}'\n"
        " WHERE label LIKE 'rdr229-stamp-%' AND completed_at IS NULL AND paused_at IS NULL;\n"
    )


def generate_unpause() -> str:
    """Releases the rows generate_pause held, after the next P3.1 deploy; a hold someone else set (another paused_by) stays."""
    return (
        "UPDATE nexus.data_moves SET paused_at = NULL, paused_by = NULL\n"
        f" WHERE label LIKE 'rdr229-stamp-%' AND paused_by = '{PAUSED_BY}';\n"
    )


def _embedded(text: str, begin: str, end: str) -> str | None:
    """The SQL inside the ```sql fence between two markers, or None when the markers or the fence are missing."""
    m = re.search(re.escape(begin) + r"\s*```sql\n(.*?)```\s*" + re.escape(end), text, re.S)
    return m.group(1) if m else None


def runbook_problems(runbook_text: str, list_text: str) -> list[str]:
    """What is wrong with the runbook's five embedded blocks against a fresh run; empty when it matches."""
    problems = []
    for label, begin, end, expected in (
        ("down-step", DOWN_BEGIN, DOWN_END, generate_down_step(list_text)),
        ("re-add", READD_BEGIN, READD_END, generate_readd(list_text)),
        ("verification query", VERIFY_BEGIN, VERIFY_END, generate_verification(list_text)),
        ("pause", PAUSE_BEGIN, PAUSE_END, generate_pause()),
        ("unpause", UNPAUSE_BEGIN, UNPAUSE_END, generate_unpause()),
    ):
        found = _embedded(runbook_text, begin, end)
        if found is None:
            problems.append(f"{RUNBOOK_REL}: the {label} block ({begin} ... {end} around a ```sql fence) is missing")
        elif found != expected:
            problems.append(f"{RUNBOOK_REL}: the embedded {label} differs from a fresh run of this script")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="the tree root (default: this checkout)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--print", action="store_true", dest="print_")
    mode.add_argument("--print-readd", action="store_true", dest="print_readd")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root)
    list_text = (root / LIST_REL).read_text(encoding="utf-8")
    sql = generate_down_step(list_text)
    readd = generate_readd(list_text)
    if args.print_:
        sys.stdout.write(sql)
        return 0
    if args.print_readd:
        sys.stdout.write(readd)
        return 0
    if args.write:
        (root / SQL_REL).write_text(sql, encoding="utf-8")
        (root / READD_REL).write_text(readd, encoding="utf-8")
        (root / READD_DB_REL).write_text(readd, encoding="utf-8")
        sys.stdout.write(f"wrote {SQL_REL}: {sql.count('DROP CONSTRAINT')} statements\n")
        sys.stdout.write(f"wrote {READD_REL} and {READD_DB_REL}: {readd.count('ADD CONSTRAINT')} statements\n")
        return 0
    problems = []
    committed = root / SQL_REL
    if not committed.exists():
        problems.append(f"{SQL_REL} is missing: run with --write")
    elif committed.read_text(encoding="utf-8") != sql:
        problems.append(f"{SQL_REL} differs from a fresh run: run with --write")
    for rel in (READD_REL, READD_DB_REL):
        committed_readd = root / rel
        if not committed_readd.exists():
            problems.append(f"{rel} is missing: run with --write")
        elif committed_readd.read_text(encoding="utf-8") != readd:
            problems.append(f"{rel} differs from a fresh run: run with --write")
    runbook = root / RUNBOOK_REL
    if not runbook.exists():
        problems.append(f"{RUNBOOK_REL} is missing")
    else:
        problems += runbook_problems(runbook.read_text(encoding="utf-8"), list_text)
    for p in problems:
        sys.stdout.write(p + "\n")
    sys.stdout.write(f"rdr229_p31_downstep: {len(problems)} problem(s), {sql.count('DROP CONSTRAINT')} constraints on the list\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
