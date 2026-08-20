# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-194 P0 census: mechanical FK/chash/doc_id/tenant-PK/TTL census
(bead nexus-tk070). Runs ``scripts/sql/fk_census.sql`` against the pytest
engine substrate's freshly Liquibase-applied schema and asserts:

(a) the script parses and returns rows for all five result sets;
(b) known ground truths, verified directly against the changelog and
    Java call sites during RDR-194 research, so the census cannot
    silently regress to vacuous (an empty or malformed result set would
    still "pass" a bare "did it run" check):

    - ``fk_catalog_chunks_chunk`` (catalog-029-manifest-chunk-fk.xml) is
      VALIDATED — catalog-029-2 runs a bare ``VALIDATE CONSTRAINT`` after
      the anti-join remediation step.
    - ``nexus.topic_assignments.doc_id`` carries the composite
      ``topic_assignments_chunk_fk`` FK, VALIDATED (RDR-194 P3d,
      ``taxonomy-012-doc-id-chunk-fk.xml`` — moved from the needs_design
      class this ground truth pinned pre-P3d), and is ``bytea`` (RDR-194
      P3c, ``taxonomy-011-doc-id-bytea.xml``; the column is a chunk chash,
      not a tumbler, RDR-194 Problem Statement item 2 / Q1).
    - ``nexus.catalog_links.from_tumbler`` (and ``to_tumbler``) carry NO
      FK today (nexus-ysrwi: 277 dangling rows measured 2026-07-25,
      RDR-194 Problem Statement item 5 / Q2).
    - ``nexus.migration_jobs`` no longer exists (reworked nexus-tk070.p5b,
      2026-08-20, Sam disposition: the table was dead — zero producers/
      consumers — so ``migration-002-tenant-pk.xml`` DROPs it outright
      rather than closing the tenant-keying gap this ground truth used to
      pin: PK=(job_id) only, no tenant_id in the PK, no tenant-scoped
      UNIQUE constraint, RLS-only protection; RDR-194 Problem Statement
      item 3).

Uses the same ``psql -tAc`` substrate-query helper already established by
``tests/db/test_schema_type_hygiene_preflight.py`` (verbatim copy, per
that file's own docstring pointing at itself as the canonical source).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

_CENSUS_SQL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sql" / "fk_census.sql"


def _psql_csv(state: dict, sql: str) -> list[str]:
    """Run *sql* against the substrate's PG via psql, CSV output, header
    stripped by ``--no-align`` off (kept aligned off, tuples only would
    hide column boundaries needed to check specific fields, so this uses
    ``-A -F','`` CSV instead of ``-tAc``'s single-column-friendly shape).
    """
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql),
            "-h", "127.0.0.1",
            "-p", str(state["pg_port"]),
            "-U", state["pg_user"],
            "-d", state["pg_dbname"],
            "-t", "-A", "-F,", "-c", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"psql failed ({proc.returncode}) running:\n{sql}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return proc.stdout.splitlines()


def _run_census_file(state: dict) -> str:
    """Run the whole census file via ``psql -f`` and return raw stdout.

    Using ``-f`` (not per-statement ``-c``) proves the file itself is
    valid, executable, multi-statement SQL — exactly how a human or CI
    step would invoke it — not just that individually-extracted snippets
    parse.
    """
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql),
            "-h", "127.0.0.1",
            "-p", str(state["pg_port"]),
            "-U", state["pg_user"],
            "-d", state["pg_dbname"],
            "-A", "-F,",
            "-f", str(_CENSUS_SQL_PATH),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"fk_census.sql failed ({proc.returncode}) via psql -f:\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return proc.stdout


@pytest.fixture(scope="module")
def census_state():
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — laziness contract, see module docstring

    return ensure_engine()


def test_fk_census_sql_file_exists():
    assert _CENSUS_SQL_PATH.is_file(), f"census script missing: {_CENSUS_SQL_PATH}"


def test_fk_census_runs_and_is_non_vacuous(census_state):
    """The census script parses as valid multi-statement SQL against a
    freshly Liquibase-applied schema and produces output. A parse error,
    or zero total output lines, means the census silently found nothing
    — exactly the vacuous-green failure mode this test exists to catch.
    """
    output = _run_census_file(census_state)
    non_empty_lines = [ln for ln in output.splitlines() if ln.strip()]
    assert len(non_empty_lines) > 20, (
        f"fk_census.sql produced suspiciously few output lines "
        f"({len(non_empty_lines)}) across all five result sets — "
        "the census may be silently matching nothing"
    )


def test_join_column_census_nonempty(census_state):
    """Result set 1 (join-column census) alone must return a substantial
    number of rows — the schema has dozens of *_id / tumbler / chash /
    collection columns; a near-empty result means the WHERE clause
    heuristic regressed."""
    sql = """
    WITH scope_schemas AS (SELECT unnest(ARRAY['nexus','t1','staging']) AS schema_name)
    SELECT count(*) FROM information_schema.columns c
    JOIN scope_schemas s ON s.schema_name = c.table_schema
    WHERE c.column_name ~ '_id$'
       OR c.column_name IN ('tumbler','from_tumbler','to_tumbler','doc_id','chash',
                             'chunk_id','collection','physical_collection','parent_id',
                             'topic_id','job_id','tenant_id');
    """
    rows = _psql_csv(census_state, sql)
    count = int(rows[0])
    assert count >= 30, f"expected >=30 join-column candidates by the raw name heuristic, got {count}"


def test_ground_truth_fk_catalog_chunks_chunk_is_validated(census_state):
    """catalog-029-2 runs VALIDATE CONSTRAINT on fk_catalog_chunks_chunk
    (catalog_document_chunks.chash) after the anti-join remediation step —
    it must show up as fk_enforced (validated=true) in the census, never
    fk_not_valid."""
    sql = """
    SELECT con.convalidated
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE con.conname = 'fk_catalog_chunks_chunk'
      AND n.nspname = 'nexus' AND c.relname = 'catalog_document_chunks';
    """
    rows = _psql_csv(census_state, sql)
    assert rows, "fk_catalog_chunks_chunk constraint not found on nexus.catalog_document_chunks"
    assert rows[0] == "t", f"expected fk_catalog_chunks_chunk to be VALIDATED, convalidated={rows[0]!r}"


def test_ground_truth_topic_assignments_doc_id_is_fk_enforced(census_state):
    """topic_assignments.doc_id moved from needs_design to fk_enforced at
    RDR-194 P3d (bead nexus-tk070.p3d, taxonomy-012-doc-id-chunk-fk.xml):
    the composite FK topic_assignments_chunk_fk (tenant_id,
    source_collection, doc_id) -> nexus.chunks (tenant_id, collection,
    chash) is added and VALIDATEd in the same migration walk (catalog-029
    three-step shape), so a freshly-migrated schema always sees it
    VALIDATED, never NOT VALID. doc_id itself is bytea (RDR-194 P3c,
    taxonomy-011-doc-id-bytea.xml) holding a chunk chash — Problem
    Statement item 2 / Q1. Ground truth PRIOR to this phase (needs_design,
    no FK) is preserved in this test's own git history; do not resurrect
    it here as a second assertion — a column carries exactly one FK
    ground truth at a time."""
    sql = """
    SELECT con.conname, con.convalidated
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
    WHERE con.contype = 'f'
      AND n.nspname = 'nexus' AND c.relname = 'topic_assignments'
      AND a.attname = 'doc_id';
    """
    rows = _psql_csv(census_state, sql)
    assert rows, "expected topic_assignments.doc_id to carry the topic_assignments_chunk_fk FK, found none"
    conname, convalidated = rows[0].split(",")
    assert conname == "topic_assignments_chunk_fk", f"unexpected FK name on doc_id: {conname!r}"
    assert convalidated == "t", (
        f"expected topic_assignments_chunk_fk to be VALIDATED on a freshly-migrated schema "
        f"(catalog-029 three-step shape ships all three steps in one walk), convalidated={convalidated!r}"
    )

    type_sql = (
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema='nexus' AND table_name='topic_assignments' AND column_name='doc_id'"
    )
    type_rows = _psql_csv(census_state, type_sql)
    assert type_rows[0] == "bytea", f"expected topic_assignments.doc_id to be bytea (RDR-194 P3c), got {type_rows[0]!r}"


def test_ground_truth_catalog_links_tumbler_columns_are_fk_enforced(census_state):
    """catalog_links.(from_tumbler,to_tumbler) moved from needs_design (no
    FK, nexus-ysrwi: 277 dangling rows measured live 2026-07-25, Problem
    Statement item 5 / Q2) to fk_enforced at RDR-194 Phase P1 (Decision D2,
    bead nexus-tk070.p1, catalog-032-links-tumbler-fk.xml):
    fk_catalog_links_from_document / fk_catalog_links_to_document, each
    ON DELETE CASCADE ON UPDATE CASCADE to nexus.catalog_documents
    (tenant_id, tumbler), added and VALIDATEd in the same migration walk
    (catalog-029 three-step shape), so a freshly-migrated schema always
    sees both VALIDATED, never NOT VALID. Ground truth PRIOR to this phase
    (needs_design, no FK) is preserved in this test's own git history; do
    not resurrect it here as a second assertion — same one-ground-truth-
    per-column discipline as the topic_assignments.doc_id sibling test
    above."""
    sql = """
    SELECT a.attname, con.conname, con.convalidated
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
    WHERE con.contype = 'f'
      AND n.nspname = 'nexus' AND c.relname = 'catalog_links'
      AND a.attname IN ('from_tumbler', 'to_tumbler')
    ORDER BY a.attname;
    """
    rows = _psql_csv(census_state, sql)
    by_column = {r.split(",")[0]: r.split(",") for r in rows}
    assert set(by_column) == {"from_tumbler", "to_tumbler"}, (
        f"expected both from_tumbler and to_tumbler to carry an FK, found columns {sorted(by_column)}"
    )
    _, from_conname, from_validated = by_column["from_tumbler"]
    _, to_conname, to_validated = by_column["to_tumbler"]
    assert from_conname == "fk_catalog_links_from_document", f"unexpected FK name on from_tumbler: {from_conname!r}"
    assert to_conname == "fk_catalog_links_to_document", f"unexpected FK name on to_tumbler: {to_conname!r}"
    assert from_validated == "t", (
        f"expected fk_catalog_links_from_document to be VALIDATED on a freshly-migrated "
        f"schema (catalog-029 three-step shape ships all three steps in one walk), "
        f"convalidated={from_validated!r}"
    )
    assert to_validated == "t", (
        f"expected fk_catalog_links_to_document to be VALIDATED on a freshly-migrated "
        f"schema (catalog-029 three-step shape ships all three steps in one walk), "
        f"convalidated={to_validated!r}"
    )


def test_ground_truth_topics_tenant_scoped_unique_present(census_state):
    """RDR-194 P5a (bead nexus-tk070.p5a, ``taxonomy-014-1``):
    ``nexus.topics`` gains ``UNIQUE (tenant_id, id)`` (constraint
    ``topics_tenant_id_unique``) so the four repointed tenant-scoped FKs
    below have a composite target to reference. Per the bead p7
    correction (2026-08-20, p5a critic [22965] S3): the anticipated
    "pin only the UNIQUE if cc5 held the repoint back" branch did NOT
    fire — p5a shipped all five changesets (the UNIQUE plus all four
    repoints) unconditionally on develop, guarded instead by a runtime
    fail-loud cross-tenant check and the pre-tag
    ``check_rdr194_cc5_delivery_gate.py`` script. This test pins the
    UNIQUE alone; the four FK ground truths follow below."""
    sql = """
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE con.contype = 'u'
      AND n.nspname = 'nexus' AND c.relname = 'topics';
    """
    rows = _psql_csv(census_state, sql)
    assert rows, "expected nexus.topics to carry a UNIQUE constraint, found none"
    assert rows[0] == "topics_tenant_id_unique", f"unexpected UNIQUE constraint name: {rows[0]!r}"

    cols_sql = """
    SELECT a.attname
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
    WHERE con.conname = 'topics_tenant_id_unique'
    ORDER BY k.ord;
    """
    cols = _psql_csv(census_state, cols_sql)
    assert cols == ["tenant_id", "id"], f"expected topics_tenant_id_unique on (tenant_id, id), got {cols!r}"


def _assert_tenant_scoped_fk_validated(census_state, table: str, column: str, expected_conname: str) -> None:
    """Shared helper for the four RDR-194 P5a (taxonomy-014-2..5) tenant-
    scoped FK ground truths below: same catalog-029 three-step shape
    (ADD ... NOT VALID -> fail-loud anti-join -> VALIDATE), all in one
    changeset per FK, so a freshly-migrated schema always sees each
    repointed constraint VALIDATED, never NOT VALID."""
    sql = f"""
    SELECT con.conname, con.convalidated,
           string_agg(a.attname, '+' ORDER BY k.ord) AS conkey_columns
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
    WHERE con.contype = 'f'
      AND n.nspname = 'nexus' AND c.relname = '{table}'
    GROUP BY con.conname, con.convalidated
    HAVING string_agg(a.attname, '+' ORDER BY k.ord) LIKE '%{column}%';
    """
    rows = _psql_csv(census_state, sql)
    assert rows, f"expected {table}.{column} to carry an FK, found none"
    conname, convalidated, conkey_columns = rows[0].split(",")
    assert conname == expected_conname, (
        f"unexpected FK name on {table}.{column}: {conname!r} (expected {expected_conname!r})"
    )
    # code-review [22995] Important: the pin must assert tenant-SCOPING, not
    # just the constraint's name — a regression that re-created a same-named
    # single-column FK (dropping tenant_id from conkey) would otherwise pass.
    assert set(conkey_columns.split("+")) == {"tenant_id", column}, (
        f"expected {expected_conname} conkey columns exactly {{tenant_id, {column}}}, "
        f"got {conkey_columns!r} — the tenant scoping is the whole point of the repoint"
    )
    assert convalidated == "t", (
        f"expected {expected_conname} to be VALIDATED on a freshly-migrated schema "
        f"(catalog-029 three-step shape ships all three steps in one changeset), "
        f"convalidated={convalidated!r}"
    )


def test_ground_truth_topics_parent_id_repointed_tenant_scoped(census_state):
    """RDR-194 D4 / P5a (``taxonomy-014-2``): the self-referential
    ``topics_parent_fk`` (``parent_id`` -> ``topics.id``, no tenant
    scoping) is DROPPED and replaced by ``fk_topics_parent_tenant``
    (``tenant_id, parent_id`` -> ``topics (tenant_id, id)``),
    VALIDATED in the same changeset."""
    _assert_tenant_scoped_fk_validated(
        census_state, "topics", "parent_id", "fk_topics_parent_tenant",
    )


def test_ground_truth_topic_assignments_topic_id_repointed_tenant_scoped(census_state):
    """RDR-194 D4 / P5a (``taxonomy-014-3``): ``topic_assignments_topic_id_fkey``
    is DROPPED and replaced by ``fk_topic_assignments_topic_tenant``
    (``tenant_id, topic_id`` -> ``topics (tenant_id, id)``), VALIDATED
    in the same changeset."""
    _assert_tenant_scoped_fk_validated(
        census_state, "topic_assignments", "topic_id", "fk_topic_assignments_topic_tenant",
    )


def test_ground_truth_topic_links_from_and_to_topic_repointed_tenant_scoped(census_state):
    """RDR-194 D4 / P5a (``taxonomy-014-4``, ``taxonomy-014-5``): both
    ``topic_links_from_topic_id_fkey`` and ``topic_links_to_topic_id_fkey``
    are DROPPED and replaced by ``fk_topic_links_from_topic_tenant`` /
    ``fk_topic_links_to_topic_tenant`` (each ``tenant_id, *_topic_id`` ->
    ``topics (tenant_id, id)``), VALIDATED in the same changeset --
    mirrors the two-column ``catalog_links`` sibling test's shape
    above (one-ground-truth-per-column, both endpoints in one test)."""
    _assert_tenant_scoped_fk_validated(
        census_state, "topic_links", "from_topic_id", "fk_topic_links_from_topic_tenant",
    )
    _assert_tenant_scoped_fk_validated(
        census_state, "topic_links", "to_topic_id", "fk_topic_links_to_topic_tenant",
    )


def test_ground_truth_migration_jobs_table_dropped(census_state):
    """nexus.migration_jobs no longer exists on a freshly-migrated schema.

    SUPERSEDES test_ground_truth_migration_jobs_has_no_tenant_scoped_uniqueness
    (which pinned the tenant-keying gap this table used to have: PK=(job_id)
    with no tenant-scoped UNIQUE, RLS-only protection). Reworked
    nexus-tk070.p5b (2026-08-20, Sam disposition): the table is DEAD —
    MigrationHandler.java / MigrationJobRepository.java were deleted at
    7bcf29c67 (2026-07-24), zero producers/consumers — so
    migration-002-tenant-pk.xml DROPs it outright rather than widening its
    PK. The tenant-keying gap this test used to document no longer applies
    to a table that does not exist; this ground truth now pins the
    retirement itself, so a future changelog that accidentally re-creates
    the table (or a rollback left mid-applied) is caught here.
    """
    exists_sql = """
    SELECT to_regclass('nexus.migration_jobs') IS NULL;
    """
    rows = _psql_csv(census_state, exists_sql)
    assert rows[0] == "t", (
        f"expected nexus.migration_jobs to be ABSENT on a freshly-migrated schema "
        f"(migration-002-tenant-pk.xml drops it), got to_regclass IS NULL = {rows[0]!r}"
    )
