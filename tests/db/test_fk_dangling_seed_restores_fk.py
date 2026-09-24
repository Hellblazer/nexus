# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression guard (tests-db-isolation): a dangling-manifest seed must not
leave ``fk_catalog_chunks_chunk`` NOT VALID for the rest of the process.

``tests/_catalog_fixture_ops.fk_dropped_for_dangling_seed`` drops the
manifest-chunk FK, lets a test write a manifest row with no backing chunk,
and re-adds the FK ``NOT VALID`` so the dangling row can survive the block.
The substrate is process-memoized (``ensure_engine``), so before this guard
the NOT VALID flag and the dangling row outlived the test that seeded them.
``tests/db/test_fk_census.py::test_ground_truth_fk_catalog_chunks_chunk_is_validated``
then read ``convalidated='f'`` whenever it shared a process with
``tests/db/test_c2_manifest_null_collection_engine.py`` (a serial
``pytest tests/db`` run); CI shards the suite, which usually separated the
two files, so CI stayed green.

The two tests below are that colliding pair in one file, so no sharding can
separate them. Order is file order (the suite has no random-order plugin):
the first test seeds and proves the seed took effect, the second checks
that the per-test restore in ``tests/conftest.py`` put the schema back.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests._catalog_fixture_ops import ActiveCatalog, fk_dropped_for_dangling_seed

_CONVALIDATED_SQL = (
    "SELECT con.convalidated FROM pg_constraint con "
    "JOIN pg_class c ON c.oid = con.conrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE con.conname = 'fk_catalog_chunks_chunk' "
    "AND n.nspname = 'nexus' AND c.relname = 'catalog_document_chunks';"
)

_DANGLING_COUNT_SQL = (
    "SELECT count(*) FROM nexus.catalog_document_chunks d "
    "WHERE NOT EXISTS (SELECT 1 FROM nexus.chunks c "
    "WHERE c.tenant_id = d.tenant_id AND c.collection = d.collection "
    "AND c.chash = d.chash);"
)


def _psql_scalar(sql: str) -> str:
    from tests._engine_substrate import _DBNAME, ensure_engine  # noqa: PLC0415 — laziness contract, as in test_fk_census.py

    state = ensure_engine()
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", os.environ["USER"], "-d", _DBNAME, "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}\nSQL: {sql}"
    return proc.stdout.strip()


def test_seed_leaves_a_dangling_row_and_a_not_valid_fk_within_the_test() -> None:
    """Non-vacuity half: inside the seeding test the dangling row exists and
    the FK is NOT VALID. Without this the restore check below could pass on
    a seed that never happened."""
    cat = ActiveCatalog()
    owner = cat.register_owner("fk-iso-guard-1", "curator")
    tumbler = cat.register(owner, "fk isolation guard doc", content_type="knowledge")
    chash = f"{0xF15A0:064x}"
    with fk_dropped_for_dangling_seed():
        cat.write_manifest(
            str(tumbler),
            [{"chash": chash, "position": 0, "chunk_index": None,
              "line_start": None, "line_end": None, "char_start": None, "char_end": None}],
            collection="knowledge__fk-iso-guard-1__bge-base-en-v15-768__v1",
        )
    assert _psql_scalar(_CONVALIDATED_SQL) == "f"
    assert int(_psql_scalar(_DANGLING_COUNT_SQL)) >= 1


def test_fk_is_validated_again_after_the_seeding_test() -> None:
    """The restore half: the next test sees the schema Liquibase built, with
    the FK VALIDATED and no dangling manifest row left behind."""
    assert _psql_scalar(_CONVALIDATED_SQL) == "t", (
        "fk_catalog_chunks_chunk is still NOT VALID after the test that "
        "seeded a dangling row finished: the seed leaked into the process"
    )
    assert _psql_scalar(_DANGLING_COUNT_SQL) == "0"
