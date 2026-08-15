# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-vacuity test for the ``scripts/sql/schema_type_hygiene_preflight_tier_{a,b,
b_hook_failures}.sql`` probes (nexus-cefa1.1, P0 pre-flight audit of the schema
type-hygiene arc; the third file split out at P2, nexus-cefa1.3 -- see below).

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
partial-migration state) and ``..._tier_b.sql`` (originally the 6
jsonb-target columns — converted across FOUR SEPARATE future phases, P2-P5,
so this file CAN be partially migrated for long stretches). nexus-cefa1.3 /
P2 (telemetry-004-type-hygiene.xml) shipped the first Tier-B conversion
(hook_failures.batch_doc_ids), which — exactly as the original header
predicted — broke whole-file execution of ``..._tier_b.sql``'s UNION ALL the
moment that column converted; its branch was split out into
``..._tier_b_hook_failures.sql`` (a single-column file), leaving
``..._tier_b.sql`` auditing the remaining FIVE still-text columns.

This test reads ``information_schema.columns.data_type`` for each of the 10
audited columns FIRST and classifies it against ``_COLUMN_EXPECTATIONS``
below:
  - expected == "text": the column is asserted to still BE text (catching
    drift the moment a future migration ships without updating this list),
    then folded into its SQL file's malformed-seed audit IF (and only if)
    EVERY column that file's UNION ALL touches is still text — the SQL
    files are whole-file UNION ALL statements; the moment one column in a
    file converts, running the WHOLE file breaks (see
    schema_type_hygiene_preflight_tier_b.sql's own header), so the
    malformed-seed audit for that file is then RETIRED here rather than run
    against invalid SQL. ``_tier_is_all_text`` gates the two atomic tiers
    (A, and pre-split B); ``_columns_all_text`` gates the two post-split
    Tier-B files independently by their own explicit column lists.
  - expected == a real PG type name (e.g. "timestamp with time zone",
    "jsonb"): the column is asserted to ALREADY be that type — the
    non-vacuous replacement for what a lesser implementation would leave as
    a silent skip. A converted column that is NOT its expected type is
    exactly as loud a failure as a not-yet-converted column that
    unexpectedly IS.

ONE-LINE EDIT PER PHASE (P2..P5): when a phase's own changeset ships, flip
that column's expected value in ``_COLUMN_EXPECTATIONS`` from ``"text"`` to
its converted PG type name (``"jsonb"`` for every Tier-B column — P2/
hook_failures.batch_doc_ids already flipped). If that flip leaves its file's
UNION ALL with a MIX of text and converted columns, that file must be split
further at that point (drop the newly-converted column's UNION ALL branch
into its own file, add its own explicit column-list constant, and gate it
via ``_columns_all_text`` the same way the P2 split did) before its
malformed-seed audit can run again — this test will correctly stop running
that file's malformed-seed audit the moment the mix appears, rather than
executing broken SQL. THE STILL-TEXT COLUMNS' AUDIT MUST KEEP RUNNING: a
split retargets, it does not retire, the surviving file's malformed-seed
coverage (see ``_assert_tier_b_malformed_seed``'s P2 retarget to
document_aspects.extras below).

PRISTINE-BASELINE ASSUMPTION, STATED EXPLICITLY: this is a SHARED substrate
-- many other tests in the same worker process write real rows into
``nexus.catalog_documents`` / ``nexus.hook_failures`` / ``nexus.
document_aspects`` (and the other audited columns) for their own tenants
before, during, and after this test runs, so ``total_rows`` is NOT expected
to be 0 at any point and is never asserted here. What IS asserted as a
pristine-baseline zero is narrower and verified true by inspection (grep
swept ``tests/`` for raw ``INSERT INTO nexus.catalog_documents`` / ``INSERT
INTO nexus.hook_failures`` / ``INSERT INTO nexus.document_aspects``,
2026-08-15: three raw catalog_documents inserts exist --
test_o8dil7_prune_misclassified_manifest_antijoin_engine.py,
test_http_aspects_stores_integration.py,
test_http_taxonomy_store_integration.py -- and NONE of them sets
``indexed_at``; no raw hook_failures or document_aspects insert exists;
every other write to these tables goes through the real HTTP/JSON write
path, which never emits malformed JSON) -- that ``invalid_cast_count`` for
hook_failures.batch_doc_ids (pre-P2, historical) and document_aspects.extras
(nexus-cefa1.3's new malformed-seed target) is 0 before this test seeds
anything.

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
_TIER_B_HOOK_FAILURES_SQL = _SQL_DIR / "schema_type_hygiene_preflight_tier_b_hook_failures.sql"

# nexus-cefa1.3: hook_failures.batch_doc_ids (P2) shipped and was split out of
# _TIER_B_SQL into its own single-column file — see that file's own header and
# schema_type_hygiene_preflight_tier_b.sql's updated header for the rationale.
# Tier B is now MIXED (5 still-text columns + 1 already-jsonb column), so the
# per-tier "_tier_is_all_text" gate below is no longer sufficient on its own;
# these two column groups gate their respective SQL files independently.
_TIER_B_COLUMNS: list[tuple[str, str]] = [
    ("plans", "plan_json"),
    ("plans", "default_bindings"),
    ("topic_links", "link_types"),
    ("document_aspects", "extras"),
    ("document_aspects", "salient_sentences"),
]
_TIER_B_HOOK_FAILURES_COLUMNS: list[tuple[str, str]] = [
    ("hook_failures", "batch_doc_ids"),
]

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
    ("B", "hook_failures", "batch_doc_ids", "jsonb"),
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


def _columns_all_text(cols: list[tuple[str, str]]) -> bool:
    """Like ``_tier_is_all_text`` but scoped to an explicit (table, column)
    list rather than a whole tier label -- needed once a tier's SQL file has
    been split (nexus-cefa1.3: Tier B split into the 5-column file and the
    1-column hook_failures file) so each split file gates independently
    instead of the whole tier retiring the moment ANY one column in it
    converts."""
    lookup = {(t, c): exp for _tier, t, c, exp in _COLUMN_EXPECTATIONS}
    return all(lookup[(t, c)] == "text" for t, c in cols)


def test_probe_fires_on_seeded_malformed_rows(t2_service_env):
    """BEFORE: the columns this test touches show zero invalid/non-ISO
    counts. Seeds malformed rows into a fresh throwaway tenant. AFTER: the
    probe's counts move by exactly the expected amount for each column and
    each malformed shape -- never asserted as vacuous ">= 1", the delta is
    pinned to the specific number of rows of each shape this test inserted.

    Only runs a tier's (or split-file's) malformed-seed audit while EVERY
    column it covers is still text (see module docstring) -- Tier A is
    currently retired here (test_column_types_match_migration_state covers
    it instead, non-vacuously); Tier B's five still-text columns run in
    full; the hook_failures.batch_doc_ids single-column split (P2 shipped,
    nexus-cefa1.3) is retired the same way Tier A was, for the same reason.
    """
    from tests._engine_substrate import ensure_engine

    state = ensure_engine()
    tenant = t2_service_env

    ran_any_tier = False

    if _tier_is_all_text("A"):
        ran_any_tier = True
        _assert_tier_a_malformed_seed(state, tenant)

    if _columns_all_text(_TIER_B_COLUMNS):
        ran_any_tier = True
        _assert_tier_b_malformed_seed(state, tenant)

    if _columns_all_text(_TIER_B_HOOK_FAILURES_COLUMNS):
        ran_any_tier = True
        _assert_tier_b_hook_failures_malformed_seed(state, tenant)

    assert ran_any_tier, (
        "no column group is fully text any more — Tier A, Tier B, and the "
        "hook_failures split have all fully migrated. This test's "
        "malformed-seed coverage is now fully retired; if a future Tier C "
        "(or a re-opened group) is added, wire its own "
        "_assert_*_malformed_seed helper and a _tier_is_all_text / "
        "_columns_all_text guard the same way rather than leaving this test "
        "silently passing on nothing"
    )


def _assert_tier_b_malformed_seed(state: dict, tenant: str) -> None:
    """nexus-cefa1.3: retargeted from hook_failures.batch_doc_ids (P2
    shipped, now audited instead by test_column_types_match_migration_state
    plus the retired _assert_tier_b_hook_failures_malformed_seed below) to
    document_aspects.extras -- one of the five columns still text in this
    split file, exercising the identical NULLIF(col,'')::jsonb shape this
    tier's own future ALTER will use."""
    before = _run_probe(state, _TIER_B_SQL, expected_rows=5)
    extras_before = _row(before, "document_aspects", "extras")

    assert extras_before["invalid_cast_count"] == 0, (
        "pristine-baseline assumption violated: some other path already "
        "wrote non-JSON document_aspects.extras -- see this file's module "
        "docstring PRISTINE-BASELINE ASSUMPTION section"
    )

    # document_aspects.collection carries a NOT VALID FK to catalog_collections
    # (tenant_id, name) -- document_aspects_collection_fk, fk-003-1 -- so the
    # collection must be registered first or the INSERT below fails loud with a
    # foreign key violation (verified empirically: this test originally omitted
    # this step and hit exactly that).
    coll = "cefa1-preflight-doc-aspects"
    _psql(state, (
        "INSERT INTO nexus.catalog_collections (tenant_id, name) "
        f"VALUES ('{tenant}', '{coll}') ON CONFLICT DO NOTHING"
    ))

    # document_aspects rows require these NOT NULL columns (aspects-001-baseline.xml).
    common_cols = (
        "tenant_id, collection, source_path, extracted_at, model_version, "
        "extractor_name, extras"
    )
    # A non-JSON string into document_aspects.extras.
    _psql(state, (
        f"INSERT INTO nexus.document_aspects ({common_cols}) VALUES ("
        f"'{tenant}', '{coll}', 'cefa1-preflight-probe-garbage', "
        "now(), 'cefa1-preflight-v1', 'cefa1-preflight', 'not-json-at-all')"
    ))
    # Empty-string sentinel: must count as empty_string_count, NOT invalid_cast_count.
    _psql(state, (
        f"INSERT INTO nexus.document_aspects ({common_cols}) VALUES ("
        f"'{tenant}', '{coll}', 'cefa1-preflight-probe-empty', "
        "now(), 'cefa1-preflight-v1', 'cefa1-preflight', '')"
    ))

    after = _run_probe(state, _TIER_B_SQL, expected_rows=5)
    extras_after = _row(after, "document_aspects", "extras")

    assert extras_after["invalid_cast_count"] == extras_before["invalid_cast_count"] + 1
    assert extras_after["empty_string_count"] == extras_before["empty_string_count"] + 1
    assert extras_after["total_rows"] == extras_before["total_rows"] + 2


def _assert_tier_b_hook_failures_malformed_seed(state: dict, tenant: str) -> None:
    """Retained for the day hook_failures.batch_doc_ids is text again (e.g. a
    rollback of telemetry-004-type-hygiene.xml) -- NOT reachable on develop
    today (P2 has shipped, _columns_all_text(_TIER_B_HOOK_FAILURES_COLUMNS)
    is False): this body is currently dead code, kept deliberately the same
    way _assert_tier_a_malformed_seed is, because the SQL it drives
    (schema_type_hygiene_preflight_tier_b_hook_failures.sql) is still shipped
    for pre-P2 clusters and still documents the probe shape. It last passed
    against a pre-telemetry-004 substrate (the same content this function
    exercised as part of the original _assert_tier_b_malformed_seed, before
    the P2 split)."""
    before = _run_probe(state, _TIER_B_HOOK_FAILURES_SQL, expected_rows=1)
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

    after = _run_probe(state, _TIER_B_HOOK_FAILURES_SQL, expected_rows=1)
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
