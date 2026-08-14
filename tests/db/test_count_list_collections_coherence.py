# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w84ho: HTTP-level integration coverage for the count()/list_collections
cross-endpoint agreement invariant + ``nx collection reindex``'s existence
check, against a mixed-dim collection.

Context (nexus-hz89h / T2 ``nexus/critique-nexus-oizh7-dim-guard-count-cross-
endpoint-break.md`` [22539], DECISION verified [22540]): ``count()`` is
DELIBERATELY dim-agnostic -- a collection total across all dims, matching
``GET /v1/vectors/stats``' dim-summed ``list_collections()`` total for the
same name. ``PgVectorRepositoryDimGuardTest.java``
(``count_isDimAgnostic_countsOwnDimAndForeignDimRows``) pins this at the
REPOSITORY layer with a mixed-dim fixture (one own-dim row via the real write
path, one foreign-dim row via raw superuser SQL). Nothing pinned it through
the HTTP boundary: ``HttpVectorClient.count()`` / ``.list_collections()`` /
``._count_or_key_error``'s "a list_collections-enumerated name never hits the
zero-count branch" contract, and ``nx collection reindex``'s existence check
(``collection.py`` ~:483-487, ``db.collection_info(name)`` inside a
try/except KeyError) -- both depend on count() staying dim-agnostic, and a
regression to dim-scoped counting would only be visible from THIS side of the
wire (the client sees the wrong number; the repository-level pin cannot see
the HTTP/JSON round trip at all).

RAW SQL, VERIFIED (not assumed): Python can issue SQL directly against the
substrate -- ``tests/_engine_substrate.py:585-599`` deliberately exports
``pg_port``/``pg_user``/``pg_dbname``/``pg_bin`` "so a test can query the
substrate's schema DIRECTLY rather than through the engine's HTTP surface"
(nexus-20890). ``_psql`` below is the same helper, copied verbatim, used by
``tests/test_o8dil7_prune_misclassified_manifest_antijoin_engine.py`` and
``tests/catalog/test_collection_scoped_tables_schema_parity.py``. ``pg_user``
is ``os.environ["USER"]`` -- the initdb role (superuser) -- so the raw insert
below bypasses RLS deliberately, mirroring
``PgVectorRepositoryDimGuardTest.java``'s own fixture construction (the ONLY
way to build a mixed-dim row: every application write path dispatches
through ``dimForCollection`` and would never write a foreign-dim column
under a collection name that resolves to a different dim).
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

# code__* -> voyage-code-3/voyage-context-3/voyage-3 all resolve to 1024 dims
# server-side (PgVectorRepository.MODEL_DIMS); bge-base-en-v15-768 resolves to
# 768. Own-dim writes go through this name's real dispatch (768); the
# foreign-dim row below is raw-inserted at embedding_1024 instead.
_COLLECTION = "code__w84ho-mixeddim__bge-base-en-v15-768__v1"


def _psql(state: dict, sql: str) -> list[str]:
    """Run *sql* against the substrate's PG, returning stripped non-empty rows."""
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


def test_count_list_collections_and_reindex_existence_agree_on_mixed_dim_collection(
    t2_service_env,
):
    """Seeds a mixed-dim collection through the engine substrate (one row at
    its own dispatched dim via the real HTTP write path, one at a foreign dim
    via raw superuser SQL -- same fixture shape as
    ``PgVectorRepositoryDimGuardTest.java``), then asserts the three
    HTTP-boundary invariants nexus-hz89h's DECISION depends on:

    (a) ``client.count(name)`` agrees with ``list_collections()``'s
        dim-summed total for the same name;
    (b) ``_count_or_key_error`` (and the public ``collection_info`` /
        ``collection_metadata`` it backs) does not raise KeyError-shape for a
        real, non-zero mixed-dim collection;
    (c) ``nx collection reindex``'s existence-check path -- ``db.
        collection_info(name)`` via the SAME ``_t3()`` factory the CLI
        command calls -- resolves the collection rather than raising
        KeyError (which the command surfaces as "collection not found").
    """
    from tests._engine_substrate import ensure_engine
    import nexus.db.http_vector_client as hvc

    state = ensure_engine()
    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)

    own_content = f"w84ho mixed-dim own-dim chunk {tenant}"
    own_chash = hashlib.sha256(own_content.encode()).hexdigest()
    foreign_chash = hashlib.sha256(
        f"w84ho mixed-dim foreign-dim chunk {tenant}".encode()
    ).hexdigest()

    # Own-dim row via the real application write path (server-side embeds at
    # bge-768's dispatched dim). This ALSO registers nexus.catalog_collections
    # for _COLLECTION, satisfying the FK the foreign-dim raw insert below
    # relies on -- same ordering PgVectorRepositoryDimGuardTest.java uses.
    client.upsert_chunks_with_embeddings(
        _COLLECTION, ids=[own_chash], documents=[own_content], embeddings=[],
        metadatas=[{"title": "w84ho-own-dim", "chunk_text_hash": own_chash}],
    )

    present = client.get_collection(_COLLECTION).get(ids=[own_chash], include=[])
    assert own_chash in (present.get("ids") or []), (
        "precondition: the own-dim row was actually written by the real "
        "write path before the mixed-dim assertions below mean anything"
    )

    # Foreign-dim row: same (tenant, collection), a DISTINCT chash, but
    # embedding_1024 populated instead of embedding_768 -- the exactly_one_
    # embedding CHECK (vectors-004-unify-chunks.xml) guarantees this is one
    # row with exactly one populated embedding column.
    vec_1024 = "[" + ",".join(["0.1"] * 1024) + "]"
    _psql(state, (
        "INSERT INTO nexus.chunks "
        "(tenant_id, collection, chash, chunk_text, embedding_1024, metadata, created_at) "
        f"VALUES ('{tenant}', '{_COLLECTION}', decode('{foreign_chash}', 'hex'), "
        f"'foreign-dim chunk text', '{vec_1024}'::vector, '{{}}'::jsonb, now())"
    ))

    # ── (a) count() <-> list_collections() cross-endpoint agreement ────────
    total = client.count(_COLLECTION)
    assert total == 2, (
        "count() must be dim-agnostic and equal the collection's TOTAL "
        "across dims (own-dim bge-768 row + foreign-dim embedding_1024 "
        "row) -- nexus-hz89h DECIDED semantics, now pinned at the HTTP "
        f"boundary. Got: {total!r}"
    )

    listed = {c["name"]: c["count"] for c in client.list_collections()}
    assert listed.get(_COLLECTION) == 2, (
        "list_collections() (GET /v1/vectors/stats, dim-summed over "
        "collection_vector_stats -- one row per (tenant, collection, dim)) "
        "must report the SAME total count() reports for the same "
        f"collection name. Got: {listed.get(_COLLECTION)!r}"
    )

    # ── (b) zero-count-means-absent contract does not misfire ──────────────
    info = client.collection_info(_COLLECTION)
    assert info["count"] == 2, (
        "collection_info (backed by _count_or_key_error) must resolve the "
        f"mixed-dim collection to its true total, not raise or under-count. Got: {info!r}"
    )
    # White-box: _count_or_key_error is the exact function the bead names as
    # the load-bearing invariant holder (its own docstring: "a
    # list_collections-enumerated name can never hit the zero-count
    # branch"). Calling it directly asserts that guarantee, not just its
    # public wrapper's behavior.
    assert client._count_or_key_error(_COLLECTION) == 2

    # ── (c) nx collection reindex's existence-check path ────────────────────
    # collection.py's reindex_cmd calls db.collection_info(name) inside a
    # try/except KeyError -> "collection not found" ClickException. Exercise
    # the SAME factory (_t3()) the command calls, not a subprocess.
    from nexus.commands.store import _t3

    db = _t3()
    reindex_info = db.collection_info(_COLLECTION)
    assert reindex_info["count"] == 2, (
        "nx collection reindex's existence check (db.collection_info) must "
        "resolve a mixed-dim collection without raising KeyError -- a "
        "regression to dim-scoped count() would make this branch see a "
        "smaller (or, for a foreign-dim-only reindex target, absent) "
        f"collection. Got: {reindex_info!r}"
    )

    # ── Falsification: prove this test would catch the hz89h regression ────
    # Contrast the DIM-SCOPED (own-dim-only) count against the dim-agnostic
    # total. A regression back to dim-scoped counting would report 1 here,
    # not 2 -- exactly the CRITICAL cross-endpoint break nexus-hz89h
    # reversed (T2 [22539]).
    dim_scoped_own = _psql(state, (
        "SELECT COUNT(*) FROM nexus.chunks WHERE tenant_id = "
        f"'{tenant}' AND collection = '{_COLLECTION}' AND embedding_768 IS NOT NULL"
    ))
    dim_scoped_own_count = int(dim_scoped_own[0]) if dim_scoped_own else 0
    assert dim_scoped_own_count == 1, (
        "precondition: the own-dim (768) row is exactly one row -- if this "
        "is not 1 the contrast below is meaningless"
    )
    assert dim_scoped_own_count != total, (
        "FALSIFICATION: the dim-SCOPED count (own-dim only, computed "
        "directly against nexus.chunks) must differ from the dim-AGNOSTIC "
        "total client.count() returns. If a future change made count() "
        "dim-scoped again, this test's own total == 2 assertion above "
        "would fail first, reporting total == 1 -- this second assertion "
        "makes explicit WHY that failure would be the hz89h regression, "
        f"not a fixture bug (dim-scoped={dim_scoped_own_count!r}, total={total!r})"
    )
