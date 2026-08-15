# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-vacuity test for the ``scripts/sql/schema_type_hygiene_preflight_tier_{a,b}.sql``
probes (nexus-cefa1.1, P0 pre-flight audit of the schema type-hygiene arc).

Proves the probe FIRES on deliberately malformed data rather than assuming
it works from reading the SQL. Runs the shipped probe files verbatim via
``psql -f`` (RLS/tenant-visibility caveats are each probe file's own header;
this test sidesteps them entirely by using the substrate's OS-superuser
connection — ``tests/_engine_substrate.py``'s ``pg_port``/``pg_user``, trust
auth, no RLS at all -- the same seam ``tests/db/test_yu9w5_assign_from_
chashes_integration.py`` and ``tests/test_o8dil7_prune_misclassified_
manifest_antijoin_engine.py`` already use for raw substrate SQL), so it is a
faithful stand-in for option (a) in each probe file's own RLS discussion.

Malformed rows are seeded ONLY under the fresh tenant ``t2_service_env``
mints for this test (the substrate's throwaway-tenant isolation contract --
see that fixture's docstring: "Tests never share or clean up state", each
test gets its own unused tenant id) -- never against shared/seeded data, and
the whole hermetic PG cluster is torn down at session end
(``tests/_engine_substrate.py::_teardown``), so nothing here is durable.

nexus-cefa1.2 / nexus-irxcq — SCHEMA-AWARENESS. The original combined probe
(scripts/sql/schema_type_hygiene_preflight.sql, retired) assumed all 10
audited columns were still TEXT. That assumption broke the moment
catalog-031-type-hygiene.xml (nexus-cefa1.2) landed: the four Tier-A
timestamp columns are timestamptz now, and every ``col = ''`` / ``col !~
...`` / ``pg_input_is_valid(col,'timestamptz')`` predicate against them
raises a Postgres type error on a migrated cluster instead of returning zero
rows. The probe is split into ``schema_type_hygiene_preflight_tier_a.sql``
(the 4 timestamp columns — ALL converted together by one changeset, no
partial-migration state) and ``..._tier_b.sql`` (the 6 jsonb-target columns
— converted across FOUR SEPARATE future phases, P2-P5, so this file CAN be
partially migrated for long stretches).

This test reads ``information_schema.columns.data_type`` for each of the 10
audited columns FIRST and classifies it against ``_COLUMN_EXPECTATIONS``
below:
  - expected == "text": the column is asserted to still BE text (catching
    drift the moment a future migration ships without updating this list),
    then folded into that tier's malformed-seed audit IF (and only if)
    EVERY column in that tier is still text — the SQL files are whole-tier
    UNION ALL statements; the moment one column in a tier converts, running
    the WHOLE file for that tier breaks (see schema_type_hygiene_preflight_
    tier_b.sql's own header), so the malformed-seed audit for that tier is
    then RETIRED here rather than run against invalid SQL.
  - expected == a real PG type name (e.g. "timestamp with time zone"): the
    column is asserted to ALREADY be that type — the non-vacuous
    replacement for what a lesser implementation would leave as a silent
    skip. A converted column that is NOT its expected type is exactly as
    loud a failure as a not-yet-converted column that unexpectedly IS.

ONE-LINE EDIT PER PHASE (P2..P5): when a phase's own changeset ships, flip
that column's expected value in ``_COLUMN_EXPECTATIONS`` from ``"text"`` to
its converted PG type name (``"jsonb"`` for every Tier-B column). If that
flip leaves its tier with a MIX of text and converted columns, the tier's
SQL file must be split further at that point (drop the newly-converted
column's UNION ALL branch into its own file) before its malformed-seed audit
can run again — this test will correctly stop running that tier's
malformed-seed audit the moment the mix appears, rather than executing
broken SQL.

PRISTINE-BASELINE ASSUMPTION, STATED EXPLICITLY: this is a SHARED substrate
-- many other tests in the same worker process write real rows into
``nexus.catalog_documents`` / ``nexus.hook_failures`` (and the other audited
columns) for their own tenants before, during, and after this test runs, so
``total_rows`` is NOT expected to be 0 at any point and is never asserted
here. What IS asserted as a pristine-baseline zero is narrower and verified
true by inspection (grep swept ``tests/`` for raw ``INSERT INTO nexus.
catalog_documents`` / ``INSERT INTO nexus.hook_failures``, 2026-08-15: three
raw catalog_documents inserts exist -- test_o8dil7_prune_misclassified_
manifest_antijoin_engine.py, test_http_aspects_stores_integration.py,
test_http_taxonomy_store_integration.py -- and NONE of them sets
``indexed_at``; no raw hook_failures insert exists; every other write to
these tables goes through the real HTTP/JSON write path, which never emits
malformed JSON) -- that ``invalid_cast_count`` for hook_failures.
batch_doc_ids is 0 before this test seeds anything.

A GENUINE PROBE FINDING, historical (Tier A no longer runs its malformed-
seed audit post-migration, but the finding stays true and stays documented
in schema_type_hygiene_preflight_tier_a.sql's own header): PostgreSQL's
timestamp/timestamptz input grammar recognizes a small set of relative
keywords ('yesterday', 'today', 'tomorrow', 'now', 'epoch', 'infinity',
'-infinity', 'allballs') as VALID input -- so
``pg_input_is_valid('yesterday', 'timestamptz')`` is TRUE even though
'yesterday' plainly fails the lexical ``!~ '^[0-9]{4}-'`` ISO-prefix check.
"""
from __future__ import annotations

import csv
import io
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

_SQL_DIR = Path(__file__).resolve().parents[2] / "scripts" / "sql"
_TIER_A_SQL = _SQL_DIR / "schema_type_hygiene_preflight_tier_a.sql"
_TIER_B_SQL = _SQL_DIR / "schema_type_hygiene_preflight_tier_b.sql"

# (tier, table, column, expected) — expected is "text" (not yet migrated,
# still audited) or the live PG type name it must already be (converted,
# non-vacuously verified). ONE-LINE EDIT per phase: flip the 4th element
# from "text" to the converted type once that column's own changeset ships
# (see module docstring "ONE-LINE EDIT PER PHASE").
_COLUMN_EXPECTATIONS: list[tuple[str, str, str, str]] = [
    ("A", "catalog_documents", "indexed_at", "timestamp with time zone"),
    ("A", "catalog_documents", "bib_enriched_at", "timestamp with time zone"),
    ("A", "catalog_documents", "index_started_at", "timestamp with time zone"),
    ("A", "catalog_links", "created_at", "timestamp with time zone"),
    ("B", "plans", "plan_json", "text"),
    ("B", "plans", "default_bindings", "text"),
    ("B", "topic_links", "link_types", "text"),
    ("B", "document_aspects", "extras", "text"),
    ("B", "document_aspects", "salient_sentences", "text"),
    ("B", "hook_failures", "batch_doc_ids", "text"),
]


def _psql(state: dict, sql: str) -> list[str]:
    """Run *sql* against the substrate's PG, returning stripped non-empty rows.

    Verbatim copy of the helper already used by
    ``tests/catalog/test_collection_scoped_tables_schema_parity.py``,
    ``tests/db/test_yu9w5_assign_from_chashes_integration.py``, and
    ``tests/test_o8dil7_prune_misclassified_manifest_antijoin_engine.py``.
    ``pg_user`` is ``os.environ["USER"]`` -- the initdb role (superuser) --
    so this bypasses RLS deliberately.
    """
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql),
            "-h", "127.0.0.1",
            "-p", str(state["pg_port"]),
            "-U", state["pg_user"],
            "-d", state["pg_dbname"],
            "-tAc", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"psql failed ({proc.returncode}) running:\n{sql}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _actual_data_type(state: dict, table: str, column: str) -> str:
    """Live ``information_schema.columns.data_type`` for (table, column) in
    the ``nexus`` schema — the schema-awareness primitive every other check
    in this file is built from."""
    rows = _psql(state, (
        "SELECT data_type FROM information_schema.columns "
        f"WHERE table_schema = 'nexus' AND table_name = '{table}' "
        f"AND column_name = '{column}'"
    ))
    assert rows, f"nexus.{table}.{column} not found in information_schema.columns"
    return rows[0]


def _run_probe(state: dict, sql_file: Path, expected_rows: int) -> list[dict]:
    """Run *sql_file* verbatim (``psql -f ... --csv``) and parse its
    one-row-per-column output into dicts keyed by the probe's own column
    names. Integer fields are parsed to ``int``; ``non_iso_prefix_count`` is
    ``None`` for Tier-B rows (the probe emits a literal SQL NULL there)."""
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql),
            "-h", "127.0.0.1",
            "-p", str(state["pg_port"]),
            "-U", state["pg_user"],
            "-d", state["pg_dbname"],
            "-v", "ON_ERROR_STOP=1",
            "--csv",
            "-f", str(sql_file),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"probe SQL failed ({proc.returncode}) running {sql_file.name}:\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    reader = csv.DictReader(io.StringIO(proc.stdout))
    rows = []
    for r in reader:
        rows.append({
            "tier": r["tier"],
            "table_name": r["table_name"],
            "column_name": r["column_name"],
            "total_rows": int(r["total_rows"]),
            "null_count": int(r["null_count"]),
            "empty_string_count": int(r["empty_string_count"]),
            "non_iso_prefix_count": (
                int(r["non_iso_prefix_count"]) if r["non_iso_prefix_count"] != "" else None
            ),
            "invalid_cast_count": int(r["invalid_cast_count"]),
        })
    assert len(rows) == expected_rows, (
        f"{sql_file.name} must emit exactly one row per audited column; "
        f"expected {expected_rows}, got {len(rows)}: {rows}"
    )
    return rows


def _row(rows: list[dict], table: str, column: str) -> dict:
    for r in rows:
        if r["table_name"] == table and r["column_name"] == column:
            return r
    raise AssertionError(f"probe emitted no row for {table}.{column}: {rows}")


def test_column_types_match_migration_state(t2_service_env):
    """Non-vacuous replacement for a skip: EVERY audited column gets a real
    assertion against its LIVE type, whichever side of its migration it is
    on. A column marked "text" that has already converted (a maintainer
    forgot to flip its _COLUMN_EXPECTATIONS entry) fails here just as loudly
    as a column marked "converted" that has not."""
    from tests._engine_substrate import ensure_engine

    state = ensure_engine()

    for tier, table, column, expected in _COLUMN_EXPECTATIONS:
        actual = _actual_data_type(state, table, column)
        assert actual == expected, (
            f"nexus.{table}.{column} (Tier {tier}): expected data_type "
            f"{expected!r} per _COLUMN_EXPECTATIONS, got {actual!r} — "
            "either a migration landed without updating this list, or this "
            "list is stale relative to the actual schema state"
        )


def _tier_is_all_text(tier: str) -> bool:
    return all(exp == "text" for t, _tbl, _col, exp in _COLUMN_EXPECTATIONS if t == tier)


def test_probe_fires_on_seeded_malformed_rows(t2_service_env):
    """BEFORE: the columns this test touches show zero invalid/non-ISO
    counts. Seeds malformed rows into a fresh throwaway tenant. AFTER: the
    probe's counts move by exactly the expected amount for each column and
    each malformed shape -- never asserted as vacuous ">= 1", the delta is
    pinned to the specific number of rows of each shape this test inserted.

    Only runs a tier's malformed-seed audit while EVERY column in that tier
    is still text (see module docstring) -- Tier A is currently retired here
    (test_column_types_match_migration_state covers it instead, non-
    vacuously); Tier B currently runs in full.
    """
    from tests._engine_substrate import ensure_engine

    state = ensure_engine()
    tenant = t2_service_env

    ran_any_tier = False

    if _tier_is_all_text("A"):
        ran_any_tier = True
        _assert_tier_a_malformed_seed(state, tenant)

    if _tier_is_all_text("B"):
        ran_any_tier = True
        _assert_tier_b_malformed_seed(state, tenant)

    assert ran_any_tier, (
        "no tier is fully text any more — both Tier A and Tier B have fully "
        "migrated. This test's malformed-seed coverage is now fully retired; "
        "if a future Tier C (or a re-opened Tier A/B) is added, wire its own "
        "_assert_tier_*_malformed_seed helper and _tier_is_all_text guard "
        "the same way rather than leaving this test silently passing on "
        "nothing"
    )


def _assert_tier_b_malformed_seed(state: dict, tenant: str) -> None:
    before = _run_probe(state, _TIER_B_SQL, expected_rows=6)
    hook_before = _row(before, "hook_failures", "batch_doc_ids")

    assert hook_before["invalid_cast_count"] == 0, (
        "pristine-baseline assumption violated: some other path already "
        "wrote non-JSON hook_failures.batch_doc_ids -- see this file's "
        "module docstring PRISTINE-BASELINE ASSUMPTION section"
    )

    # A non-JSON string into hook_failures.batch_doc_ids.
    _psql(state, (
        "INSERT INTO nexus.hook_failures (tenant_id, hook_name, batch_doc_ids) "
        f"VALUES ('{tenant}', 'cefa1-preflight-probe-garbage', 'not-json-at-all')"
    ))
    # Empty-string sentinel: must count as empty_string_count, NOT invalid_cast_count.
    _psql(state, (
        "INSERT INTO nexus.hook_failures (tenant_id, hook_name, batch_doc_ids) "
        f"VALUES ('{tenant}', 'cefa1-preflight-probe-empty', '')"
    ))

    after = _run_probe(state, _TIER_B_SQL, expected_rows=6)
    hook_after = _row(after, "hook_failures", "batch_doc_ids")

    assert hook_after["invalid_cast_count"] == hook_before["invalid_cast_count"] + 1
    assert hook_after["empty_string_count"] == hook_before["empty_string_count"] + 1
    assert hook_after["total_rows"] == hook_before["total_rows"] + 2


def _assert_tier_a_malformed_seed(state: dict, tenant: str) -> None:
    """Retained for the day Tier A (or a future re-split tier of the same
    shape) is text again -- e.g. a rollback. NOT reachable on develop today
    (catalog-031 has shipped, _tier_is_all_text("A") is False): this body is
    currently dead code, kept deliberately because the SQL it drives
    (schema_type_hygiene_preflight_tier_a.sql) is still shipped for
    pre-031 clusters (the cloud pre-flight ran it) and still documents the
    GENUINE PROBE FINDING this seeds against. It last passed against a
    pre-031 substrate at commit ac49908d5.
    """
    before = _run_probe(state, _TIER_A_SQL, expected_rows=4)
    doc_before = _row(before, "catalog_documents", "indexed_at")

    assert doc_before["non_iso_prefix_count"] == 0, (
        "pristine-baseline assumption violated: some other path already "
        "wrote a non-ISO catalog_documents.indexed_at value"
    )
    assert doc_before["invalid_cast_count"] == 0

    # (1) a PG relative-keyword: fails the lexical ISO-prefix check but is
    #     ACCEPTED by pg_input_is_valid(...,'timestamptz') -- see
    #     schema_type_hygiene_preflight_tier_a.sql's GENUINE PROBE FINDING.
    _psql(state, (
        "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, indexed_at) "
        f"VALUES ('{tenant}', 'cefa1-preflight-relword', 'cefa1 preflight probe doc', 'yesterday')"
    ))
    # (2) genuinely un-parseable garbage: fails BOTH checks.
    _psql(state, (
        "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, indexed_at) "
        f"VALUES ('{tenant}', 'cefa1-preflight-garbage', 'cefa1 preflight probe doc 2', 'not-a-timestamp-at-all')"
    ))
    # (3) the empty-string sentinel: neither check should fire; only empty_string_count.
    _psql(state, (
        "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, indexed_at) "
        f"VALUES ('{tenant}', 'cefa1-preflight-empty', 'cefa1 preflight probe doc 3', '')"
    ))

    after = _run_probe(state, _TIER_A_SQL, expected_rows=4)
    doc_after = _row(after, "catalog_documents", "indexed_at")

    # TWO rows fail the lexical check ('yesterday' + garbage); only ONE of
    # those two also fails the real cast (garbage; 'yesterday' is a valid PG
    # relative-timestamp keyword); ONE empty-string row.
    assert doc_after["non_iso_prefix_count"] == doc_before["non_iso_prefix_count"] + 2
    assert doc_after["invalid_cast_count"] == doc_before["invalid_cast_count"] + 1
    assert doc_after["empty_string_count"] == doc_before["empty_string_count"] + 1
    assert doc_after["total_rows"] == doc_before["total_rows"] + 3
