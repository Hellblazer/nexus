# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-vacuity test for ``scripts/sql/schema_type_hygiene_preflight.sql``
(nexus-cefa1.1, P0 pre-flight audit of the schema type-hygiene arc).

Proves the probe FIRES on deliberately malformed data rather than assuming
it works from reading the SQL. Runs the shipped probe file verbatim via
``psql -f`` (RLS/tenant-visibility caveats are the probe file's own header;
this test sidesteps them entirely by using the substrate's OS-superuser
connection — ``tests/_engine_substrate.py``'s ``pg_port``/``pg_user``, trust
auth, no RLS at all -- the same seam ``tests/db/test_yu9w5_assign_from_
chashes_integration.py`` and ``tests/test_o8dil7_prune_misclassified_
manifest_antijoin_engine.py`` already use for raw substrate SQL), so it is a
faithful stand-in for option (a) in the probe file's own RLS discussion.

Malformed rows are seeded ONLY under the fresh tenant ``t2_service_env``
mints for this test (the substrate's throwaway-tenant isolation contract --
see that fixture's docstring: "Tests never share or clean up state", each
test gets its own unused tenant id) -- never against shared/seeded data, and
the whole hermetic PG cluster is torn down at session end
(``tests/_engine_substrate.py::_teardown``), so nothing here is durable.

PRISTINE-BASELINE ASSUMPTION, STATED EXPLICITLY: this is a SHARED substrate
-- many other tests in the same worker process write real rows into
``nexus.catalog_documents`` / ``nexus.hook_failures`` (and the other 8
audited columns) for their own tenants before, during, and after this test
runs, so ``total_rows`` is NOT expected to be 0 at any point and is never
asserted here. What IS asserted as a pristine-baseline zero is narrower and
verified true by inspection (grep swept ``tests/`` for raw
``INSERT INTO nexus.catalog_documents`` / ``INSERT INTO nexus.hook_failures``,
2026-08-15: three raw catalog_documents inserts exist --
test_o8dil7_prune_misclassified_manifest_antijoin_engine.py,
test_http_aspects_stores_integration.py, test_http_taxonomy_store_integration.py
-- and NONE of them sets ``indexed_at`` (nullable, no default, lands NULL,
excluded by the probe's own guard); no raw hook_failures insert exists; every
other write to these tables goes through the real HTTP/JSON write path, which
never emits malformed JSON or a non-ISO timestamp string) -- that ``invalid_cast_count``
and ``non_iso_prefix_count`` for the two columns this test touches are 0
before this test seeds anything. If a future test starts raw-inserting
malformed values into these same two columns, this test's BEFORE assertions
(not its delta assertions) would be the ones to fail first.

A GENUINE PROBE FINDING surfaces in this test rather than being assumed
away: PostgreSQL's timestamp/timestamptz input grammar recognizes a small
set of relative keywords ('yesterday', 'today', 'tomorrow', 'now', 'epoch',
'infinity', '-infinity', 'allballs') as VALID input -- so
``pg_input_is_valid('yesterday', 'timestamptz')`` is TRUE even though
'yesterday' plainly fails the lexical ``!~ '^[0-9]{4}-'`` ISO-prefix check.
The two Tier-A counts are answering different questions
(``non_iso_prefix_count``: "does this look like an ISO timestamp?";
``invalid_cast_count``: "will the real ALTER's USING cast on this value
actually raise?") and must not be conflated -- a relative-keyword value
would silently convert to a moving, current-time-relative timestamp under
the eventual ``ALTER COLUMN ... USING NULLIF(col,'')::timestamptz``, which
is a real semantic risk distinct from an abort-on-cast risk. This test seeds
one row of each shape so the distinction is demonstrated, not asserted on
faith.
"""
from __future__ import annotations

import csv
import io
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

_PROBE_SQL = (
    Path(__file__).resolve().parents[2] / "scripts" / "sql"
    / "schema_type_hygiene_preflight.sql"
)


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


def _run_probe(state: dict) -> list[dict]:
    """Run the shipped probe file verbatim (``psql -f ... --csv``) and parse
    its one-row-per-column output into dicts keyed by the probe's own column
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
            "-f", str(_PROBE_SQL),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"probe SQL failed ({proc.returncode}):\n"
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
    assert len(rows) == 10, (
        f"probe must emit exactly one row per audited column (6 Tier-B + 4 "
        f"Tier-A = 10); got {len(rows)}: {rows}"
    )
    return rows


def _row(rows: list[dict], table: str, column: str) -> dict:
    for r in rows:
        if r["table_name"] == table and r["column_name"] == column:
            return r
    raise AssertionError(f"probe emitted no row for {table}.{column}: {rows}")


def test_probe_fires_on_seeded_malformed_rows(t2_service_env):
    """BEFORE: the two audited columns this test touches show zero invalid/
    non-ISO counts. Seeds malformed rows into a fresh throwaway tenant.
    AFTER: the probe's counts move by exactly the expected amount for each
    column and each malformed shape -- never asserted as vacuous ">= 1", the
    delta is pinned to the specific number of rows of each shape this test
    inserted.
    """
    from tests._engine_substrate import ensure_engine

    state = ensure_engine()
    tenant = t2_service_env

    before = _run_probe(state)
    hook_before = _row(before, "hook_failures", "batch_doc_ids")
    doc_before = _row(before, "catalog_documents", "indexed_at")

    assert hook_before["invalid_cast_count"] == 0, (
        "pristine-baseline assumption violated: some other path already "
        "wrote non-JSON hook_failures.batch_doc_ids -- see this file's "
        "module docstring PRISTINE-BASELINE ASSUMPTION section"
    )
    assert doc_before["non_iso_prefix_count"] == 0, (
        "pristine-baseline assumption violated: some other path already "
        "wrote a non-ISO catalog_documents.indexed_at value"
    )
    assert doc_before["invalid_cast_count"] == 0

    # ---- Tier B: a non-JSON string into hook_failures.batch_doc_ids ----
    _psql(state, (
        "INSERT INTO nexus.hook_failures (tenant_id, hook_name, batch_doc_ids) "
        f"VALUES ('{tenant}', 'cefa1-preflight-probe-garbage', 'not-json-at-all')"
    ))
    # Empty-string sentinel: must count as empty_string_count, NOT invalid_cast_count.
    _psql(state, (
        "INSERT INTO nexus.hook_failures (tenant_id, hook_name, batch_doc_ids) "
        f"VALUES ('{tenant}', 'cefa1-preflight-probe-empty', '')"
    ))

    # ---- Tier A: catalog_documents.indexed_at, three malformed shapes ----
    # (1) a PG relative-keyword: fails the lexical ISO-prefix check but is
    #     ACCEPTED by pg_input_is_valid(...,'timestamptz') -- see module
    #     docstring's GENUINE PROBE FINDING.
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

    after = _run_probe(state)
    hook_after = _row(after, "hook_failures", "batch_doc_ids")
    doc_after = _row(after, "catalog_documents", "indexed_at")

    # Tier B deltas: exactly one malformed-JSON row, exactly one empty-string row.
    assert hook_after["invalid_cast_count"] == hook_before["invalid_cast_count"] + 1
    assert hook_after["empty_string_count"] == hook_before["empty_string_count"] + 1
    assert hook_after["total_rows"] == hook_before["total_rows"] + 2

    # Tier A deltas: TWO rows fail the lexical check ('yesterday' + garbage);
    # only ONE of those two also fails the real cast (garbage; 'yesterday'
    # is a valid PG relative-timestamp keyword); ONE empty-string row.
    assert doc_after["non_iso_prefix_count"] == doc_before["non_iso_prefix_count"] + 2
    assert doc_after["invalid_cast_count"] == doc_before["invalid_cast_count"] + 1
    assert doc_after["empty_string_count"] == doc_before["empty_string_count"] + 1
    assert doc_after["total_rows"] == doc_before["total_rows"] + 3
