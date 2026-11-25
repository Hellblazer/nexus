# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two queries ``nx doctor``'s RDR-229 rows carry, run against a real store (RDR-229 P3.1, nexus-p2m1x.17).

The engine's status field gives only COUNTS of the registry's dangling tombstones (a tombstone whose ``superseded_by``
names no row of its tenant) and of its self-id rows (a tombstone whose ``superseded_by_id`` is its own ``collection_id``:
retired, no successor), because ``/v1/status`` is unauthenticated. The doctor rows give the operator a read-only query that
lists those rows by name, to run as ``nexus_admin``. A query copied into a hint can drift from the schema, so each one runs
here on the substrate's registry, seeded with one self-id row, one dangling tombstone and the rows that must NOT match
(a live collection, a tombstone whose successor exists and carries its id), and must return exactly the seeded row.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from nexus.rdr229_completion_health import DANGLING_QUERY, SELF_ID_QUERY
from tests._chunk_seed import _lit
from tests._engine_substrate import ensure_engine

_MODEL = "bge-base-en-v15-768"


def _psql(sql: str) -> list[list[str]]:
    state = ensure_engine()
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]), "-U", state["pg_user"],
         "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-A", "-t", "-F", "|", "-1", "-f", "-"],
        input=sql, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return [line.split("|") for line in proc.stdout.splitlines() if line.strip()]


def _ours(query: str, tenant: str) -> list[list[str]]:
    """What *query* returns for this test's tenant: the query text itself, unchanged, as a subselect."""
    return _psql(f"SELECT * FROM ({query.rstrip().rstrip(';')}) q WHERE q.tenant_id = {_lit(tenant)} ORDER BY 1, 2, 3;")


def test_each_query_returns_exactly_the_seeded_row_and_none_of_the_others() -> None:
    tag = uuid.uuid4().hex[:8]
    # A tenant of its own, seeded by the superuser and removed after: the test mints no tenant through the engine, so it adds
    # nothing to the tenant-minting ceiling.
    tenant = f"r7q-{tag}"

    def name(role: str) -> str:
        return f"knowledge__r7q{tag}{role}__{_MODEL}__v1"

    live, successor, stamped, self_id, dangling = (name(r) for r in ("live", "succ", "stamped", "selfid", "dangling"))
    names = [live, successor, stamped, self_id, dangling]

    def insert(n: str) -> str:
        return (
            "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, embedding_model, lifecycle_state) "
            f"VALUES ({_lit(tenant)}, {_lit(n)}, 'knowledge', '1-1', {_lit(_MODEL)}, 'live');"
        )

    def retire(n: str, by: str, id_sql: str) -> str:
        return (
            f"UPDATE nexus.catalog_collections SET superseded_by = {_lit(by)}, superseded_by_id = {id_sql} "
            f"WHERE tenant_id = {_lit(tenant)} AND name = {_lit(n)};"
        )

    own_id = "collection_id"
    successor_id = f"(SELECT collection_id FROM nexus.catalog_collections WHERE tenant_id = {_lit(tenant)} AND name = {_lit(successor)})"
    # A tombstone's id may name any row (the CHECK only wants it non-null), so the dangling case points at the successor's id:
    # it is dangling by NAME and not a self-id row, and the self-id case names a live successor but carries its own id.
    seed = "\n".join([
        *(insert(n) for n in names),
        retire(stamped, successor, successor_id),        # a healthy tombstone: successor exists, id is the successor's
        retire(self_id, successor, own_id),              # self-id row: its successor exists by name, its id is its own
        retire(dangling, f"ghost{tag}", successor_id),   # dangling: no row of this tenant carries that name
    ])
    cleanup = f"DELETE FROM nexus.catalog_collections WHERE tenant_id = {_lit(tenant)} AND name IN ({', '.join(_lit(n) for n in names)});"
    _psql(seed)
    try:
        assert _ours(DANGLING_QUERY, tenant) == [[tenant, "knowledge", dangling]]
        assert _ours(SELF_ID_QUERY, tenant) == [[tenant, "knowledge", self_id]]
        # The unfiltered query runs as written (tenant, content type and name come first, ordered) and holds the seeded rows.
        assert [tenant, "knowledge", dangling] in _psql(DANGLING_QUERY.rstrip().rstrip(";") + ";")
        assert [tenant, "knowledge", self_id] in _psql(SELF_ID_QUERY.rstrip().rstrip(";") + ";")
    finally:
        _psql(cleanup)
