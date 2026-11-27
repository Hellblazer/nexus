# SPDX-License-Identifier: AGPL-3.0-or-later
"""The manual e2e scripts that seed ``nexus.*`` by raw SQL must supply every collection id the head schema requires.

RDR-229 P3.1 (vectors-068) gave every table that names a collection an id column beside the name, and guards each pair
with a NOT VALID CHECK (``(NULLIF(<name>, '') IS NULL) = (<id> IS NULL)``): a row with a name and no id is refused with
23514, and a NOT VALID check is still enforced on every new row. ``nexus.chunks`` and ``nexus.taxonomy_centroids`` also
carry ``CHECK (collection_id IS NOT NULL)``. Three scripts under ``tests/e2e`` seed by SQL and are run by hand
(``lab-remote``), so nothing in CI ran them against the head schema and all three had gone stale (nexus-clfse.2):
``export_scale_run.py``, ``import_scale_run.py`` and ``rdr229_p2r_mvv_checks.py`` (check 6).

This test reads each script's generated seed SQL and checks it against the constraints, which it DERIVES from the
changelog (``vectors-*.xml``), not from a hand list, so a new id pair added by a later changeset is required of the
scripts too. Offline: it needs no Postgres. The live proof is the scripts' own run on the engine substrate
(see the bead's hand-back); this is the early twin that fails in the commit that changes the schema.

INTERIM. The authority for "which id columns does the head schema require" is a Java schema guard (clfse.2 plan step 3)
that reads ``pg_constraint`` and ``information_schema`` on a MIGRATED schema, so it also sees constraints a changelog regex
cannot (a later changeset, a validated or renamed check). This reader derives from changelog TEXT and is the early offline
twin until then. Plan step 5 moves the scripts onto ``pg_temp.nx_seed_*`` (service/src/test/resources/seed/nexus_seed.sql);
at that point this reader is retired. The pure SQL-builder seams it reads (``seed_registry_sql``, ``seed_chunk_batch_sql``,
``seed_other_tables_scripts``, ``document_aspects_insert_sql``) stay: step 5 reuses them.

What it checks, per INSERT into a constrained table: the id column is supplied whenever the name column is (always, once a
changelog drops the name column: P4), the id is a registry subselect, and that subselect names the SAME tenant and collection
the row names (a subselect for another collection passes the CHECK and stamps the wrong id). A source scan
(``test_no_inline_write_to_a_constrained_table_outside_the_generators``) requires every such INSERT in the three scripts to sit
inside one of the four generator functions, so a new inline write cannot bypass the reader.

The statement reader is deliberately small: it understands the shapes these scripts write (``INSERT ... VALUES`` and
``INSERT ... SELECT`` with a column list, and ``UPDATE ... SET``). A statement it cannot read raises, so an unreadable
seed is a red test, not a pass over input it did not examine.
"""
from __future__ import annotations

import ast
import importlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import click
import pytest

_REPO = Path(__file__).resolve().parents[1]
_CHANGELOG = _REPO / "service" / "src" / "main" / "resources" / "db" / "changelog"
_E2E = _REPO / "tests" / "e2e"

_NAME_ID = re.compile(
    r"ALTER TABLE nexus\.(\w+)\s+ADD CONSTRAINT (\w+) CHECK \(\(NULLIF\((\w+), ''\) IS NULL\) = \((\w+) IS NULL\)\)")
_NOT_NULL = re.compile(r"ALTER TABLE nexus\.(\w+)\s+ADD CONSTRAINT (\w+) CHECK \((\w+) IS NOT NULL\)")
_REGISTRY_SUBSELECT = re.compile(
    r"\(\s*SELECT\s+collection_id\s+FROM\s+nexus\.catalog_collections\s+WHERE\b.*\btenant_id\b.*\bname\b", re.S | re.I)


@dataclass(frozen=True)
class Requirement:
    """A table that must carry *id_column* whenever it carries *name_column* (``None``: always)."""

    table: str
    name_column: str | None
    id_column: str
    constraint: str


_ROLLBACK = re.compile(r"<rollback\b.*?</rollback>", re.S)
_DROP_COLUMN = re.compile(r"ALTER TABLE nexus\.(\w+)\s+DROP COLUMN\s+(?:IF EXISTS\s+)?(\w+)", re.I)


def requirements_from(texts: list[str]) -> list[Requirement]:
    """Every name/id pair and every collection_id NOT NULL the changelog *texts* add, deduplicated by constraint name.

    A name column that a later changeset drops (RDR-229 P4.1) no longer conditions its pair: the id is then required in
    every INSERT into the table, whether or not the row names a collection.
    """
    found: dict[str, Requirement] = {}
    dropped: set[tuple[str, str]] = set()
    for text in texts:
        for table, constraint, name_col, id_col in _NAME_ID.findall(text):
            found[constraint] = Requirement(table, name_col, id_col, constraint)
        for table, constraint, id_col in _NOT_NULL.findall(text):
            found[constraint] = Requirement(table, None, id_col, constraint)
        dropped.update(_DROP_COLUMN.findall(_ROLLBACK.sub("", text)))   # a rollback's DROP COLUMN undoes an add; it drops nothing live
    return [Requirement(r.table, None, r.id_column, r.constraint) if (r.table, r.name_column) in dropped else r
            for r in found.values()]


def head_requirements() -> list[Requirement]:
    """``requirements_from`` every changelog file."""
    return requirements_from([p.read_text(encoding="utf-8") for p in sorted(_CHANGELOG.glob("*.xml"))])


# -- a small SQL statement reader ----------------------------------------------------------------------------------------


def _scan(text: str):
    """Yield ``(index, char, depth, in_string)`` over *text*, with ``--`` comments and ``'...'`` strings understood."""
    depth, i, n = 0, 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    yield i, c, depth, True
                    i += 1
                    yield i, text[i], depth, True
                else:
                    in_str = False
                    yield i, c, depth, True
            else:
                yield i, c, depth, True
        elif c == "'":
            in_str = True
            yield i, c, depth, True
        elif c == "-" and text.startswith("--", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            if c in "([":
                depth += 1
            elif c in ")]":
                depth -= 1
            yield i, c, depth, False
        i += 1
    if in_str:
        raise ValueError("unterminated string literal in seed SQL")


def _clean(text: str) -> str:
    """*text* with ``--`` comments dropped (a comment may hold commas, parentheses and quotes)."""
    kept = {i: c for i, c, _d, _s in _scan(text)}
    return "".join(kept.get(i, "") for i in range(len(text)))


def split_statements(sql: str) -> list[str]:
    cleaned = _clean(sql)
    out, start = [], 0
    for i, c, depth, in_str in _scan(cleaned):
        if c == ";" and not in_str and depth == 0:
            out.append(cleaned[start:i])
            start = i + 1
    out.append(cleaned[start:])
    return [s.strip() for s in out if s.strip()]


def split_top(text: str) -> list[str]:
    """Split on commas that sit outside every parenthesis and string."""
    parts, start = [], 0
    for i, c, depth, in_str in _scan(text):
        if c == "," and not in_str and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def _group_at(text: str, open_index: int) -> tuple[str, int]:
    """The text inside the parenthesis opened at *open_index*, and the index just past its close."""
    assert text[open_index] == "("
    base = None
    for i, c, depth, in_str in _scan(text[open_index:]):
        if base is None:
            base = depth - 1   # depth after the opening paren
        if c == ")" and not in_str and depth == base:
            return text[open_index + 1:open_index + i], open_index + i + 1
    raise ValueError(f"unbalanced parenthesis in: {text[open_index:open_index + 80]!r}")


def _top_level_keyword(text: str, word: str) -> int:
    """Index of the first whole-word *word* outside every parenthesis and string, or ``-1``."""
    pat = re.compile(rf"\b{word}\b", re.I)
    depth_at = {i: (d, s) for i, _c, d, s in _scan(text)}
    for m in pat.finditer(text):
        d, s = depth_at.get(m.start(), (1, True))
        if d == 0 and not s:
            return m.start()
    return -1


@dataclass(frozen=True)
class Insert:
    table: str
    columns: list[str]
    rows: list[list[str]]   # one list of value expressions per row (an INSERT ... SELECT is one row)


_INSERT = re.compile(r"\bINSERT\s+INTO\s+(?:nexus\.)?(\w+)\s*\(", re.I)
_UPDATE = re.compile(r"^\s*UPDATE\s+(?:nexus\.)?(\w+)\b", re.I)


def read_inserts(statement: str) -> list[Insert]:
    out = []
    for m in _INSERT.finditer(statement):
        cols_inner, after = _group_at(statement, m.end() - 1)
        columns = [c.strip() for c in split_top(cols_inner)]
        tail = statement[after:].lstrip()
        if re.match(r"VALUES\b", tail, re.I):
            rest = tail[6:]
            rows, i = [], 0
            while True:
                j = rest.find("(", i)
                if j < 0:
                    break
                inner, i = _group_at(rest, j)
                rows.append(split_top(inner))
                follow = rest[i:].lstrip()
                if not follow.startswith(","):
                    break
        elif re.match(r"SELECT\b", tail, re.I):
            body = tail[6:]
            end = _top_level_keyword(body, "FROM")
            rows = [split_top(body if end < 0 else body[:end])]
        else:
            raise ValueError(f"cannot read the INSERT into {m.group(1)}: {tail[:60]!r}")
        for row in rows:
            if len(row) != len(columns):
                raise ValueError(f"INSERT into {m.group(1)}: {len(columns)} columns but {len(row)} values: {row}")
        out.append(Insert(m.group(1), columns, rows))
    return out


def updated_columns(statement: str) -> tuple[str, list[str]] | None:
    m = _UPDATE.match(statement)
    if not m:
        return None
    rest = statement[m.end():]
    s = re.search(r"\bSET\b", rest, re.I)
    if not s:
        raise ValueError(f"cannot read the UPDATE of {m.group(1)}")
    body = rest[s.end():]
    end = min([e for e in (_top_level_keyword(body, "WHERE"), _top_level_keyword(body, "FROM")) if e >= 0] or [len(body)])
    return m.group(1), [a.split("=")[0].strip() for a in split_top(body[:end])]


# -- the check ---------------------------------------------------------------------------------------------------------------


_SUB_NAME = re.compile(r"\bname\s*=\s*('(?:[^']|'')*'|[\w.]+)", re.I)
_SUB_TENANT = re.compile(r"\btenant_id\s*=\s*('(?:[^']|'')*'|[\w.]+)", re.I)


def _subselect_violations(ins: Insert, row: list[str], k: int, name_col: str | None, req: Requirement) -> list[str]:
    """The id expression of one row must be a registry subselect for the SAME tenant and collection the row names."""
    expr = row[k]
    where = f"INSERT INTO {ins.table}: {req.id_column}"
    if not _REGISTRY_SUBSELECT.search(expr):
        return [f"{where} is {expr[:60]!r}, not a registry subselect ({req.constraint})"]
    out = []
    pairs = [("name", _SUB_NAME, name_col), ("tenant_id", _SUB_TENANT, "tenant_id")]
    for label, pattern, column in pairs:
        if column is None or column not in ins.columns:
            continue   # no row value to compare against (a dropped name column): the shape check above is all there is
        want = row[ins.columns.index(column)].strip()
        m = pattern.search(expr)
        if m is None:
            out.append(f"{where}: the subselect has no {label} = ... to compare with {column} ({req.constraint})")
        elif m.group(1).strip() != want:
            out.append(f"{where}: the subselect stamps the id of {label} {m.group(1)}, but the row's {column} is {want} "
                       f"({req.constraint})")
    return out


def id_violations(sql: str, requirements: list[Requirement]) -> tuple[list[str], set[str]]:
    """``(violations, constrained tables examined)`` for every statement of *sql*."""
    by_table: dict[str, list[Requirement]] = {}
    for r in requirements:
        by_table.setdefault(r.table, []).append(r)
    violations: list[str] = []
    examined: set[str] = set()
    for stmt in split_statements(sql):
        for ins in read_inserts(stmt):
            table_reqs = by_table.get(ins.table, [])
            for req in table_reqs:
                has_name = req.name_column is not None and req.name_column in ins.columns
                has_id = req.id_column in ins.columns
                # An id is required when the row names the collection, and ALWAYS when the pair has no live name column
                # (the NOT NULL guards, and a name column a changelog has dropped: P4). A row that names nothing and
                # supplies no id is the one tolerated shape ('' and NULL mean unset).
                required = req.name_column is None or has_name
                if not (required or has_id):
                    continue
                examined.add(ins.table)
                if not has_id:
                    why = f"names {req.name_column}" if req.name_column else "is always required"
                    violations.append(f"INSERT INTO {ins.table} supplies no {req.id_column} ({req.constraint}: {why})")
                    continue
                # the name column the id's subselect must agree with: the pair's own, or for a NOT NULL guard the sibling
                # pair's on the same id column
                name_col = req.name_column or next(
                    (s.name_column for s in table_reqs if s.id_column == req.id_column and s.name_column), None)
                k = ins.columns.index(req.id_column)
                for row in ins.rows:
                    violations.extend(_subselect_violations(ins, row, k, name_col, req))
        upd = updated_columns(stmt)
        if upd:
            table, assigned = upd
            for req in by_table.get(table, []):
                if req.name_column in assigned and req.id_column not in assigned:
                    examined.add(table)
                    violations.append(f"UPDATE {table} sets {req.name_column} without {req.id_column} ({req.constraint})")
    return violations, examined


# -- the reader and the requirements are themselves checked --------------------------------------------------------------


def test_requirements_are_derived_from_the_changelog() -> None:
    """Non-vacuity: the derivation finds the P3.1 constraints, so an empty or broken regex cannot make every seed pass."""
    reqs = head_requirements()
    pairs = {(r.table, r.name_column, r.id_column) for r in reqs}
    assert len(reqs) >= 19, f"vectors-068 alone adds 19 name/id and NOT NULL constraints; found {len(reqs)}"
    for want in [
        ("chunks", "collection", "collection_id"), ("chunks", None, "collection_id"),
        ("catalog_documents", "physical_collection", "physical_collection_id"),
        ("catalog_document_chunks", "collection", "collection_id"),
        ("topic_assignments", "source_collection", "source_collection_id"),
        ("taxonomy_centroids", None, "collection_id"),
    ]:
        assert want in pairs, f"{want} missing from {sorted(pairs, key=str)}"


_GOOD = ("(SELECT collection_id FROM nexus.catalog_collections WHERE tenant_id = 't' AND name = 'c')")


@pytest.mark.parametrize(("sql", "needle"), [
    # a name with no id (the shape of every stale seed)
    ("INSERT INTO nexus.document_aspects (tenant_id, collection, source_path) VALUES ('t', 'c', 'p')",
     "supplies no collection_id"),
    # the id column present but NULL
    ("INSERT INTO nexus.document_aspects (tenant_id, collection, collection_id) VALUES ('t', 'c', NULL)",
     "not a registry subselect"),
    # INSERT ... SELECT, the form the scale scripts use
    ("INSERT INTO nexus.topics (tenant_id, label, collection) SELECT 't', 'l' || g, 'c' FROM generate_series(1, 3) g",
     "supplies no collection_id"),
    # the always-required id of chunks, with no name at all
    ("INSERT INTO nexus.chunks (tenant_id, chash) VALUES ('t', 'x')", "supplies no collection_id"),
    # the physical_ prefix pair
    ("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, physical_collection) VALUES ('t', '1.1', 'c')",
     "supplies no physical_collection_id"),
    # an UPDATE that moves a name and leaves the id
    ("UPDATE nexus.topics SET collection = 'd' WHERE tenant_id = 't'", "sets collection without collection_id"),
    # a registry subselect for ANOTHER collection: it satisfies the CHECK and stamps the wrong id
    (f"INSERT INTO nexus.document_aspects (tenant_id, collection, collection_id) VALUES ('t', 'other', {_GOOD})",
     "stamps the id of name 'c', but the row's collection is 'other'"),
    # ... and for another tenant
    (f"INSERT INTO nexus.document_aspects (tenant_id, collection, collection_id) VALUES ('u', 'c', {_GOOD})",
     "stamps the id of tenant_id 't', but the row's tenant_id is 'u'"),
    # the same through INSERT ... SELECT (the scale scripts' form) and through the physical_ pair
    (f"INSERT INTO nexus.topics (tenant_id, label, collection, collection_id) SELECT 't', 'l', 'other', {_GOOD} FROM generate_series(1, 3) g",
     "stamps the id of name 'c'"),
    (f"INSERT INTO nexus.catalog_documents (tenant_id, tumbler, physical_collection, physical_collection_id) VALUES ('t', '1', 'other', {_GOOD})",
     "stamps the id of name 'c'"),
    # chunks' NOT NULL guard is checked against the sibling pair's name too
    (f"INSERT INTO nexus.chunks (tenant_id, collection, chash, collection_id) VALUES ('t', 'other', 'x', {_GOOD})",
     "stamps the id of name 'c'"),
    # an id with NO name column in the row is still validated (it is not skipped for want of a name)
    ("INSERT INTO nexus.document_aspects (tenant_id, collection_id) VALUES ('t', NULL)", "not a registry subselect"),
])
def test_the_checker_catches_a_missing_id(sql: str, needle: str) -> None:
    """The control: a seed that omits or nulls an id is reported, so a green on the real seeds means something."""
    violations, examined = id_violations(sql, head_requirements())
    assert any(needle in v for v in violations), (needle, violations)
    assert examined


@pytest.mark.parametrize("sql", [
    f"INSERT INTO nexus.document_aspects (tenant_id, collection, collection_id) VALUES ('t', 'c', {_GOOD})",
    f"INSERT INTO nexus.topics (tenant_id, label, collection, collection_id) SELECT 't', 'l' || g, 'c', {_GOOD} FROM generate_series(1, 3) g",
    f"INSERT INTO nexus.catalog_documents (tenant_id, tumbler, physical_collection, physical_collection_id) VALUES ('t', '1.1', 'c', {_GOOD}), ('t', '1.2', 'c', {_GOOD})",
    # a name column left out needs no id: '' and NULL mean unset
    "INSERT INTO nexus.catalog_documents (tenant_id, tumbler) VALUES ('t', '1.1')",
    # a comment holding a comma, a quote and a parenthesis must not move a column
    f"INSERT INTO nexus.document_aspects (tenant_id, collection, collection_id) -- it's (here), really\nVALUES ('t', 'c', {_GOOD})",
])
def test_the_checker_passes_a_complete_seed(sql: str) -> None:
    violations, _ = id_violations(sql, head_requirements())
    assert violations == []


_P3 = ("ALTER TABLE nexus.widgets ADD CONSTRAINT widgets_collection_id_check "
       "CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;")
_P4 = "ALTER TABLE nexus.widgets DROP COLUMN IF EXISTS collection;"


def test_a_dropped_name_column_makes_the_id_unconditionally_required() -> None:
    """P4 drops the name columns. The guard must survive that: an INSERT with no name column and no id is then a violation."""
    before = requirements_from([_P3])
    after = requirements_from([_P3, _P4])
    bare = "INSERT INTO nexus.widgets (tenant_id, label) VALUES ('t', 'x')"
    assert id_violations(bare, before)[0] == []   # today the name is nullable: unset needs no id
    # a DROP COLUMN inside a <rollback> undoes an add and drops nothing live
    assert id_violations(bare, requirements_from([_P3, f"<rollback>{_P4}</rollback>"]))[0] == []
    violations, examined = id_violations(bare, after)
    assert any("supplies no collection_id" in v for v in violations) and examined == {"widgets"}
    # and an id that is there is validated by shape even though no name column exists to compare it with
    assert id_violations("INSERT INTO nexus.widgets (tenant_id, collection_id) VALUES ('t', NULL)", after)[0]
    ok = f"INSERT INTO nexus.widgets (tenant_id, collection_id) VALUES ('t', {_GOOD})"
    assert id_violations(ok, after)[0] == []


def test_an_unreadable_statement_is_an_error_not_a_pass() -> None:
    with pytest.raises(ValueError):
        id_violations("INSERT INTO nexus.topics (tenant_id, label) DEFAULT VALUES", head_requirements())


# -- the three scripts ---------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scripts():
    """The three e2e scripts, imported with their own directory on the path (as they import one another by bare name)."""
    saved = list(sys.path)
    sys.path.insert(0, str(_E2E))
    sys.path.insert(0, str(_REPO))
    try:
        esr = importlib.import_module("export_scale_run")
        isr = importlib.import_module("import_scale_run")
        mvv = importlib.import_module("rdr229_p2r_mvv_checks")
    finally:
        sys.path[:] = saved
    return esr, isr, mvv


def _assert_supplies_ids(label: str, sql: str, must_examine: set[str]) -> None:
    violations, examined = id_violations(sql, head_requirements())
    assert not violations, f"{label} would be refused by the head schema (23514):\n  " + "\n  ".join(violations)
    missing = must_examine - examined
    assert not missing, f"{label}: expected a constrained write to {sorted(missing)} but the reader examined only {sorted(examined)}"


def test_export_scale_run_seed_supplies_collection_ids(scripts) -> None:
    esr, _isr, _mvv = scripts
    sql = esr.seed_registry_sql(2_000) + "\n".join(esr.seed_chunk_batch_sql(lo, lo + 99) for lo in (1, 101))
    _assert_supplies_ids("export_scale_run", sql, {"catalog_documents", "chunks", "catalog_document_chunks"})


def test_import_scale_run_seed_supplies_collection_ids(scripts) -> None:
    _esr, isr, _mvv = scripts
    sql = "\n".join(script for _message, script in isr.seed_other_tables_scripts(2_000))
    _assert_supplies_ids(
        "import_scale_run", sql,
        {"document_aspects", "document_highlights", "chunk_orphaned_at", "taxonomy_meta", "topics", "topic_assignments",
         "taxonomy_centroids"})


def test_count_mismatches_names_short_and_missing_tables(scripts) -> None:
    esr, isr, _mvv = scripts
    want = esr.expected_seed_counts(1_000, 5_000)
    assert esr.count_mismatches(want, dict(want)) == []
    short = esr.count_mismatches(want, {**want, "chunks": want["chunks"] - 1, "catalog_document_chunks": 0})
    assert [m.split(":")[0] for m in short] == ["chunks", "catalog_document_chunks"]
    # a table the read-back did not return is a mismatch too (None is not the wanted count)
    assert esr.count_mismatches({"topics": 20}, {}) == ["topics: seeded None, wanted 20"]
    # the import script wants every table it writes, each with a positive count (a zero-row table would pass an id check by having no row)
    other = isr.expected_other_counts(2_000)
    assert all(v > 0 for v in other.values()), other


class _StubCluster:
    """Stands in for ``export_scale_run.Cluster``: records every statement and answers each ``count(*)`` from *counts*.

    *counts* maps a table name to the count to return; a table not in it answers ``"0"``, as an empty table would.
    """

    def __init__(self, counts: dict[str, int] | None = None) -> None:
        self.counts = counts or {}
        self.scripts: list[str] = []
        self.counted: list[str] = []

    def psql_script(self, sql: str, **_kw) -> None:
        self.scripts.append(sql)

    def psql(self, sql: str, **_kw) -> str:
        if "count(*)" not in sql:
            return ""
        self.counted.append(sql)
        table = re.search(r"FROM nexus\.(\w+)", sql).group(1)
        return str(self.counts.get(table, 0))


def test_export_seed_raises_when_it_comes_up_short_and_counts_only_stamped_rows(scripts) -> None:
    """``_seed`` must RAISE on a short seed (not record it), and must count a stamped table's rows by their id column."""
    esr, _isr, _mvv = scripts
    chunks, memory = 100, 500
    short = _StubCluster()   # every count(*) is 0, a seed that wrote nothing
    with pytest.raises(click.ClickException, match="came up short"):
        esr._seed(short, chunks, memory, 0, lambda _m: None)
    stamped = {"catalog_collections": "collection_id", "catalog_documents": "physical_collection_id",
               "chunks": "collection_id", "catalog_document_chunks": "collection_id"}
    for table, id_column in stamped.items():
        sql = next(q for q in short.counted if f"FROM nexus.{table} " in q)
        assert f"{id_column} IS NOT NULL" in sql, (table, sql)
    # the control: a cluster that returns the wanted counts does not raise, so the raise above is the count check's doing
    ok = _StubCluster({"catalog_collections": 1, "catalog_documents": max(1, chunks // 10), "chunks": chunks,
                       "catalog_document_chunks": chunks, "memory": memory})
    esr._seed(ok, chunks, memory, 0, lambda _m: None)


def test_import_seed_raises_when_it_comes_up_short_and_counts_only_stamped_rows(scripts) -> None:
    esr, isr, _mvv = scripts
    chunks = 2_000
    short = _StubCluster()
    with pytest.raises(click.ClickException, match="came up short"):
        isr._seed_other_tables(short, chunks, lambda _m: None)
    for table, id_column in isr._ID_COLUMN.items():
        sql = next(q for q in short.counted if f"FROM nexus.{table} " in q)
        assert f"{id_column} IS NOT NULL" in sql, (table, sql)
    ok = _StubCluster(isr.expected_other_counts(chunks))
    assert isr._seed_other_tables(ok, chunks, lambda _m: None) == isr.other_tables_plan(chunks)


# -- the generators are the only place these scripts write a constrained table ----------------------------------------------

#: file -> the generator functions inside which an INSERT into a constrained table is allowed (the seams the reader reads).
_GENERATORS = {
    "export_scale_run.py": {"seed_registry_sql", "seed_chunk_batch_sql"},
    "import_scale_run.py": {"seed_other_tables_scripts"},
    "rdr229_p2r_mvv_checks.py": {"document_aspects_insert_sql"},
}


def inline_writes(source: str, tables: set[str], allowed: set[str]) -> list[str]:
    """``line: text`` of every ``INSERT INTO [nexus.]<table>`` of *tables* in *source* outside the *allowed* functions."""
    tree = ast.parse(source)
    spans = [(n.lineno, n.end_lineno or n.lineno) for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name in allowed]
    pattern = re.compile(r"INSERT\s+INTO\s+(?:nexus\.)?(\w+)", re.I)
    out = []
    for lineno, line in enumerate(source.splitlines(), 1):
        for m in pattern.finditer(line):
            if m.group(1) in tables and not any(a <= lineno <= b for a, b in spans):
                out.append(f"{lineno}: {line.strip()[:100]}")
    return out


def test_the_inline_write_scan_catches_a_write_outside_the_generators() -> None:
    src = ("def seed_registry_sql():\n    return 'INSERT INTO nexus.chunks (a) VALUES (1)'\n\n"
           "def elsewhere(cluster):\n    cluster.psql('INSERT INTO nexus.chunks (a) VALUES (1)')\n"
           "    cluster.psql('insert into topics (a) values (1)')\n    cluster.psql('INSERT INTO nexus.memory (a) VALUES (1)')\n")
    found = inline_writes(src, {"chunks", "topics"}, {"seed_registry_sql"})
    assert [f.split(":")[0] for f in found] == ["5", "6"], found   # memory is unconstrained; the generator's own line is allowed


def test_no_inline_write_to_a_constrained_table_outside_the_generators() -> None:
    """A new inline ``INSERT`` into a constrained table in a script would bypass the reader above; it must live in a generator."""
    tables = {r.table for r in head_requirements()}
    found = {}
    for name, allowed in _GENERATORS.items():
        source = (_E2E / name).read_text(encoding="utf-8")
        found[name] = inline_writes(source, tables, allowed)
        # non-vacuity: the allowed functions exist, so the allowance names real code
        for fn in allowed:
            assert f"def {fn}(" in source, f"{name} has no generator {fn}"
    assert found == {name: [] for name in _GENERATORS}, found


def test_rdr229_p2r_mvv_check6_aspects_row_supplies_collection_id(scripts) -> None:
    _esr, _isr, mvv = scripts
    sql = mvv.document_aspects_insert_sql("tenant-a", "gate-stem", "abs/path/r.md", "chroma://gate-stem//abs/path/r.md", "1.1.7")
    _assert_supplies_ids("rdr229_p2r_mvv_checks check 6", sql, {"document_aspects"})
